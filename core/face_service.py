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
import os
import re
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from PIL import Image, ImageFilter, ImageOps, ImageStat
from django.core.files.base import ContentFile
from django.utils import timezone

try:
    import cv2  # type: ignore
    import numpy as np  # type: ignore
    HAS_OPENCV = True
except ImportError:
    cv2 = None
    np = None
    HAS_OPENCV = False

try:
    import face_recognition  # type: ignore
    HAS_FACE_RECOGNITION = True
except ImportError:
    face_recognition = None
    HAS_FACE_RECOGNITION = False

from .models import (
    AdvanceRequest, AuditLog, CustomUser, FaceIDLog,
    FaceIDSettings, Manager, StaffAbsenceExemption, StaffBonus, Teacher,
)

# face-api.js 128-D descriptor uchun qat'iy Evklid masofasi chegarasi (Strict Threshold)
# 0.58 o'rniga 0.45 qilib belgilandi — begona yoki boshqa xodimlar bilan adashib ketmasligi uchun
FACE_API_EUCLIDEAN_THRESHOLD = 0.45
# Minimal talab qilinadigan o'xshashlik foizi (Confidence Score)
MIN_MATCH_SIMILARITY_SCORE = 72.0
# Ikki xodim orasidagi minimal masofa farqi (Ambiguity Guard Margin)
AMBIGUITY_DISTANCE_MARGIN = 0.040


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
    Qat'iy chegara: masofa <= 0.45 bo'lsagina bir xil shaxs hisoblanadi.
    Qaytaradi: (similarity_score: 0..100, is_match: bool, euclidean_distance: float)
    """
    d1 = parse_descriptor(feat1) if not isinstance(feat1, list) else feat1
    d2 = parse_descriptor(feat2) if not isinstance(feat2, list) else feat2
    if not d1 or not d2 or len(d1) != len(d2):
        return 0.0, False, 1.0

    dist = calculate_euclidean_distance(d1, d2)
    is_match = dist <= threshold

    if is_match:
        # dist = 0.0 -> 99.9%, dist = threshold (0.45) -> 72.0%
        ratio = max(0.0, min(1.0, (threshold - dist) / max(threshold, 0.01)))
        score = round(min(99.9, 72.0 + (ratio ** 1.1) * 27.9), 1)
    else:
        # Chegaradan oshganda foiz keskin tushadi (< 60%)
        over = dist - threshold
        score = round(max(0.0, min(65.0, 65.0 - over * 90.0)), 1)

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
# 2.5. RASM FORMATI, TINIQLIGI (SHARPNESS/BLUR) VA TO'G'RI QARASH (POSE/ANGLE) VALIDATSIYASI
# ─────────────────────────────────────────────────────────────

ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
ALLOWED_PIL_FORMATS = {'JPEG', 'PNG', 'MPO'}
MIN_IMAGE_WIDTH = 160
MIN_IMAGE_HEIGHT = 160
DEFAULT_MIN_SHARPNESS = 45.0   # Minimal tiniqlik (Laplacian variance) chegarasi
MAX_YAW_ANGLE = 20.0           # Boshning chap/o'ngga burilish chegarasi (gradus)
MAX_PITCH_ANGLE = 20.0         # Boshning tepaga/pastga egilish chegarasi (gradus)
MAX_ROLL_ANGLE = 18.0          # Boshning yelkaga qiyshayish chegarasi (gradus)


def validate_image_format(image_source, filename: str | None = None) -> tuple[bool, Image.Image | None, str, str | None]:
    """
    Rasm formati (.jpg, .jpeg, .png) va minimal o'lchamini tekshiradi.
    Qaytaradi: (is_valid, pil_rgb_image, format_name, error_message)
    """
    if image_source is None:
        return False, None, '', "Rasm fayli tanlanmagan! Iltimos, .jpg, .jpeg yoki .png formatidagi rasm yuklang."

    fname = filename or getattr(image_source, 'name', '') or ''
    if fname:
        ext = os.path.splitext(str(fname))[1].lower()
        if ext and ext not in ALLOWED_IMAGE_EXTENSIONS:
            return (
                False,
                None,
                ext,
                f"Noto'g'ri rasm formati ({ext})! Faqat .jpg, .jpeg yoki .png formatidagi rasmlarni yuklashga ruxsat etiladi.",
            )

    try:
        if isinstance(image_source, Image.Image):
            pil_img = image_source
            detected_format = (pil_img.format or 'JPEG').upper()
        else:
            if hasattr(image_source, 'seek'):
                image_source.seek(0)
            raw_data = image_source.read() if hasattr(image_source, 'read') else image_source
            if hasattr(image_source, 'seek'):
                image_source.seek(0)
            if isinstance(raw_data, (bytes, bytearray)):
                pil_img = Image.open(io.BytesIO(raw_data))
            else:
                pil_img = Image.open(image_source)
            detected_format = (pil_img.format or '').upper()

        if detected_format not in ALLOWED_PIL_FORMATS:
            return (
                False,
                None,
                detected_format,
                f"Rasm formati qo'llab-quvvatlanmaydi ({detected_format or 'Nomaʼlum'})! "
                "Iltimos, faqat .jpg, .jpeg yoki .png formatidagi rasm yuklang.",
            )

        try:
            pil_img = ImageOps.exif_transpose(pil_img)
        except Exception:
            pass

        w, h = pil_img.size
        if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
            return (
                False,
                None,
                detected_format,
                f"Rasm o'lchami juda kichik ({w}x{h} piksel). "
                f"Yuzni aniq tanish uchun kamida {MIN_IMAGE_WIDTH}x{MIN_IMAGE_HEIGHT} piksel bo'lgan tiniq rasm yuklang.",
            )

        pil_rgb = pil_img.convert('RGB')
        return True, pil_rgb, detected_format, None
    except Exception:
        return (
            False,
            None,
            '',
            "Rasm fayli shikastlangan yoki uni o'qib bo'lmadi! Iltimos, to'g'ri .jpg, .jpeg yoki .png rasm tanlang.",
        )


def calculate_image_sharpness(pil_img: Image.Image, face_box: tuple[int, int, int, int] | None = None) -> float:
    """
    Rasmning (yoki yuz sohasi `face_box = (x, y, w, h)` ning) tiniqlik darajasini
    Laplacian Variance usuli yordamida hisoblaydi.
    OpenCV mavjud bo'lsa `cv2.Laplacian(gray, cv2.CV_64F).var()` ishlatiladi,
    aks holda PIL 3x3 Laplacian Kernel orqali hisoblanadi.
    """
    if pil_img is None:
        return 0.0

    w, h = pil_img.size
    if face_box and len(face_box) == 4:
        fx, fy, fw, fh = face_box
        x1 = max(0, int(fx))
        y1 = max(0, int(fy))
        x2 = min(w, int(fx + fw))
        y2 = min(h, int(fy + fh))
        roi = pil_img.crop((x1, y1, x2, y2)) if (x2 - x1 > 20 and y2 - y1 > 20) else pil_img
    else:
        # Markaziy yuz sohasini olish (orqa fon xiraligi / portret rejimi ta'sir qilmasligi uchun)
        margin_x = int(w * 0.15)
        margin_y = int(h * 0.12)
        roi = pil_img.crop((margin_x, margin_y, w - margin_x, h - margin_y))

    gray_roi = roi.convert('L')
    # Standart o'lchamga keltirish (turli megapikselli kameralarda bir xil mezon bo'lishi uchun)
    eval_w, eval_h = gray_roi.size
    if max(eval_w, eval_h) > 320:
        scale = 320.0 / float(max(eval_w, eval_h))
        gray_roi = gray_roi.resize(
            (max(32, int(eval_w * scale)), max(32, int(eval_h * scale))),
            Image.Resampling.BILINEAR,
        )

    if HAS_OPENCV and cv2 is not None and np is not None:
        gray_arr = np.array(gray_roi, dtype=np.uint8)
        lap_var = float(cv2.Laplacian(gray_arr, cv2.CV_64F).var())
        return round(lap_var, 2)

    # Pure-Python / PIL 3x3 Laplacian operator: [0, 1, 0, 1, -4, 1, 0, 1, 0]
    lap_kernel = ImageFilter.Kernel(
        size=(3, 3),
        kernel=[0, 1, 0, 1, -4, 1, 0, 1, 0],
        scale=1,
        offset=128,
    )
    lap_img = gray_roi.filter(lap_kernel)
    # Chekkadagi 1px ramkani chiqarib tashlash
    lw, lh = lap_img.size
    if lw > 4 and lh > 4:
        lap_img = lap_img.crop((1, 1, lw - 1, lh - 1))
    stat = ImageStat.Stat(lap_img)
    # PIL uint8 clipping tufayli dispersiyani OpenCV CV_64F shkalasiga moslashtiramiz
    pil_var = float(stat.var[0]) * 3.2
    return round(pil_var, 2)


def _mean_pt(points: list[tuple[float, float]]) -> tuple[float, float]:
    if not points:
        return (0.0, 0.0)
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
    )


def estimate_head_pose_from_landmarks(landmarks_data, img_size: tuple[int, int] = (480, 480)) -> dict | None:
    """
    68 nuqtali (face-api.js / dlib / face_recognition) yoki 5 nuqtali yuz landmarklaridan
    boshning burilish burchaklarini (yaw, pitch, roll — gradusda) hisoblaydi.
    - yaw:   chapga (-) / o'ngga (+) burilish
    - pitch: tepaga (-) / pastga (+) egilish
    - roll:  chap/o'ng yelkaga qiyshayish
    """
    if not landmarks_data:
        return None

    try:
        if isinstance(landmarks_data, str):
            landmarks_data = json.loads(landmarks_data)

        left_eye = right_eye = nose_tip = left_mouth = right_mouth = chin = left_jaw = right_jaw = None

        # 1-holat: 68 ta nuqtali ro'yxat (face-api.js `det.landmarks.positions`)
        if isinstance(landmarks_data, list) and len(landmarks_data) >= 68:
            pts = []
            for p in landmarks_data[:68]:
                if isinstance(p, dict):
                    pts.append((float(p.get('x', p.get('_x', 0.0))), float(p.get('y', p.get('_y', 0.0)))))
                elif isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append((float(p[0]), float(p[1])))
            if len(pts) >= 68:
                left_eye = _mean_pt(pts[36:42])
                right_eye = _mean_pt(pts[42:48])
                nose_tip = pts[30]
                left_mouth = pts[48]
                right_mouth = pts[54]
                chin = pts[8]
                left_jaw = pts[0]
                right_jaw = pts[16]

        # 2-holat: face_recognition.face_landmarks() lug'ati yoki maxsus kalitli dict
        elif isinstance(landmarks_data, dict):
            if 'positions' in landmarks_data and isinstance(landmarks_data['positions'], list):
                return estimate_head_pose_from_landmarks(landmarks_data['positions'], img_size=img_size)

            def _extract_pt(val):
                if not val:
                    return None
                if isinstance(val, dict):
                    return (float(val.get('x', 0.0)), float(val.get('y', 0.0)))
                if isinstance(val, (list, tuple)):
                    if len(val) >= 2 and isinstance(val[0], (int, float)):
                        return (float(val[0]), float(val[1]))
                    pts_list = [
                        (float(item['x']), float(item['y'])) if isinstance(item, dict) else (float(item[0]), float(item[1]))
                        for item in val if item is not None
                    ]
                    return _mean_pt(pts_list) if pts_list else None
                return None

            left_eye = _extract_pt(landmarks_data.get('left_eye') or landmarks_data.get('leftEye'))
            right_eye = _extract_pt(landmarks_data.get('right_eye') or landmarks_data.get('rightEye'))
            nose_tip = _extract_pt(landmarks_data.get('nose_tip') or landmarks_data.get('nose') or landmarks_data.get('nose_bridge'))
            left_mouth = _extract_pt(landmarks_data.get('left_mouth') or landmarks_data.get('mouth_left'))
            right_mouth = _extract_pt(landmarks_data.get('right_mouth') or landmarks_data.get('mouth_right'))
            if not left_mouth or not right_mouth:
                top_lip = landmarks_data.get('top_lip') or landmarks_data.get('mouth')
                if isinstance(top_lip, list) and len(top_lip) >= 2:
                    pts_lip = [
                        (float(p['x']), float(p['y'])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
                        for p in top_lip
                    ]
                    pts_lip_sorted = sorted(pts_lip, key=lambda item: item[0])
                    left_mouth = pts_lip_sorted[0]
                    right_mouth = pts_lip_sorted[-1]
            chin_raw = landmarks_data.get('chin') or landmarks_data.get('jaw')
            if isinstance(chin_raw, list) and len(chin_raw) >= 3:
                pts_chin = [
                    (float(p['x']), float(p['y'])) if isinstance(p, dict) else (float(p[0]), float(p[1]))
                    for p in chin_raw
                ]
                left_jaw = pts_chin[0]
                right_jaw = pts_chin[-1]
                chin = pts_chin[len(pts_chin) // 2]
            else:
                chin = _extract_pt(chin_raw)

        if not (left_eye and right_eye and nose_tip):
            return None

        # Ko'zlar teskari tushib qolgan bo'lsa tartiblash
        if left_eye[0] > right_eye[0]:
            left_eye, right_eye = right_eye, left_eye

        dx_eyes = right_eye[0] - left_eye[0]
        dy_eyes = right_eye[1] - left_eye[1]
        eye_dist = math.hypot(dx_eyes, dy_eyes)
        if eye_dist < 5.0:
            return None

        # 1. ROLL (boshning yelkaga qiyshayishi)
        roll_deg = math.degrees(math.atan2(dy_eyes, dx_eyes))

        # 2. YAW (chapga / o'ngga burilish)
        eye_mid_x = (left_eye[0] + right_eye[0]) / 2.0
        eye_mid_y = (left_eye[1] + right_eye[1]) / 2.0
        nose_offset_ratio = (nose_tip[0] - eye_mid_x) / (eye_dist * 0.5)
        yaw_deg = nose_offset_ratio * 38.0

        if left_jaw and right_jaw:
            dist_left_jaw = math.hypot(nose_tip[0] - left_jaw[0], nose_tip[1] - left_jaw[1])
            dist_right_jaw = math.hypot(right_jaw[0] - nose_tip[0], right_jaw[1] - nose_tip[1])
            jaw_sum = dist_left_jaw + dist_right_jaw
            if jaw_sum > 10.0:
                jaw_asym = (dist_left_jaw - dist_right_jaw) / jaw_sum
                yaw_deg = 0.5 * yaw_deg + 0.5 * (jaw_asym * 70.0)

        # 3. PITCH (tepaga / pastga qarash)
        if left_mouth and right_mouth:
            mouth_mid_y = (left_mouth[1] + right_mouth[1]) / 2.0
            eye_to_mouth = max(mouth_mid_y - eye_mid_y, 5.0)
            eye_to_nose = nose_tip[1] - eye_mid_y
            # To'g'ri qaralganda ko'z-burun masofasi ko'z-og'iz masofasining ~0.56 qismini tashkil etadi
            vertical_ratio = eye_to_nose / eye_to_mouth
            pitch_deg = (vertical_ratio - 0.56) * 105.0
        elif chin:
            eye_to_chin = max(chin[1] - eye_mid_y, 10.0)
            eye_to_nose = nose_tip[1] - eye_mid_y
            vertical_ratio = eye_to_nose / eye_to_chin
            pitch_deg = (vertical_ratio - 0.36) * 110.0
        else:
            pitch_deg = ((nose_tip[1] - eye_mid_y) / eye_dist - 0.60) * 75.0

        yaw_deg = max(-90.0, min(90.0, round(yaw_deg, 1)))
        pitch_deg = max(-90.0, min(90.0, round(pitch_deg, 1)))
        roll_deg = max(-90.0, min(90.0, round(roll_deg, 1)))

        return {
            'yaw': yaw_deg,
            'pitch': pitch_deg,
            'roll': roll_deg,
        }
    except Exception:
        return None


def detect_face_and_pose(pil_img: Image.Image, landmarks=None, pose_angles=None) -> dict:
    """
    Rasmda yuz borligini va boshning burchaklarini (yaw, pitch, roll) aniqlaydi.
    Ustuvorlik tartibi:
      1. Uzatilgan `pose_angles` yoki 68-nuqtali `landmarks`
      2. `face_recognition` kutubxonasi (agar o'rnatilgan bo'lsa)
      3. OpenCV (`cv2`) Haar Cascade frontalface + eye + profileface detektorlari
      4. Pure-Python / PIL simmetriya tahlili (fallback)
    """
    result = {
        'face_detected': True,
        'face_count': 1,
        'face_box': None,
        'angles': {'yaw': 0.0, 'pitch': 0.0, 'roll': 0.0},
        'method': 'fallback',
    }

    # 1. Agar burchaklar bevosita uzatilgan bo'lsa
    if pose_angles:
        try:
            if isinstance(pose_angles, str):
                pose_angles = json.loads(pose_angles)
            if isinstance(pose_angles, dict) and any(k in pose_angles for k in ('yaw', 'pitch', 'roll')):
                result['angles'] = {
                    'yaw': round(float(pose_angles.get('yaw', 0.0)), 1),
                    'pitch': round(float(pose_angles.get('pitch', 0.0)), 1),
                    'roll': round(float(pose_angles.get('roll', 0.0)), 1),
                }
                result['method'] = 'explicit_angles'
                return result
        except Exception:
            pass

    # 2. Agar landmarklar uzatilgan bo'lsa (masalan face-api.js dan)
    if landmarks:
        est = estimate_head_pose_from_landmarks(landmarks, img_size=pil_img.size if pil_img else (480, 480))
        if est is not None:
            result['angles'] = est
            result['method'] = 'landmarks'
            return result

    if pil_img is None:
        result['face_detected'] = False
        result['face_count'] = 0
        return result

    # 3. `face_recognition` kutubxonasi mavjud bo'lsa
    if HAS_FACE_RECOGNITION and face_recognition is not None and np is not None:
        try:
            rgb_arr = np.array(pil_img.convert('RGB'))
            face_locs = face_recognition.face_locations(rgb_arr, model='hog')
            if len(face_locs) == 0:
                result['face_detected'] = False
                result['face_count'] = 0
                result['method'] = 'face_recognition'
                return result
            result['face_count'] = len(face_locs)
            top, right, bottom, left = face_locs[0]
            result['face_box'] = (left, top, right - left, bottom - top)
            lm_list = face_recognition.face_landmarks(rgb_arr, face_locations=[face_locs[0]])
            if lm_list:
                est = estimate_head_pose_from_landmarks(lm_list[0], img_size=pil_img.size)
                if est:
                    result['angles'] = est
            result['method'] = 'face_recognition'
            return result
        except Exception:
            pass

    # 4. OpenCV (`cv2`) Haar Cascade mavjud bo'lsa
    if HAS_OPENCV and cv2 is not None and np is not None:
        try:
            gray_arr = np.array(pil_img.convert('L'), dtype=np.uint8)
            gray_eq = cv2.equalizeHist(gray_arr)
            haar_dir = getattr(cv2.data, 'haarcascades', '')
            frontal_cascade = cv2.CascadeClassifier(os.path.join(haar_dir, 'haarcascade_frontalface_default.xml'))
            eye_cascade = cv2.CascadeClassifier(os.path.join(haar_dir, 'haarcascade_eye.xml'))
            profile_cascade = cv2.CascadeClassifier(os.path.join(haar_dir, 'haarcascade_profileface.xml'))

            faces = frontal_cascade.detectMultiScale(
                gray_eq, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
            )
            if len(faces) == 0:
                # Yon tomondan (profil) qarab turganini tekshirish
                profiles_left = profile_cascade.detectMultiScale(
                    gray_eq, scaleFactor=1.1, minNeighbors=4, minSize=(60, 60)
                )
                profiles_right = profile_cascade.detectMultiScale(
                    cv2.flip(gray_eq, 1), scaleFactor=1.1, minNeighbors=4, minSize=(60, 60)
                )
                if len(profiles_left) > 0 or len(profiles_right) > 0:
                    result['face_detected'] = True
                    result['face_count'] = 1
                    result['angles'] = {
                        'yaw': 42.0 if len(profiles_left) > 0 else -42.0,
                        'pitch': 0.0,
                        'roll': 0.0,
                    }
                    result['method'] = 'opencv_profile'
                    return result

                result['face_detected'] = False
                result['face_count'] = 0
                result['method'] = 'opencv_haar'
                return result

            # Eng katta yuzni tanlash
            faces_sorted = sorted(faces, key=lambda b: b[2] * b[3], reverse=True)
            fx, fy, fw, fh = [int(v) for v in faces_sorted[0]]
            result['face_count'] = len(faces_sorted)
            result['face_box'] = (fx, fy, fw, fh)
            result['method'] = 'opencv_haar'

            # Yuzning yuqori yarmidan ko'zlarni qidirish
            upper_face = gray_eq[fy:fy + int(fh * 0.62), fx:fx + fw]
            eyes = eye_cascade.detectMultiScale(
                upper_face, scaleFactor=1.1, minNeighbors=4, minSize=(int(fw * 0.12), int(fh * 0.08))
            )

            yaw_deg = 0.0
            pitch_deg = 0.0
            roll_deg = 0.0

            if len(eyes) >= 2:
                eyes_sorted = sorted(eyes, key=lambda e: e[2] * e[3], reverse=True)[:2]
                eyes_sorted = sorted(eyes_sorted, key=lambda e: e[0])
                e1, e2 = eyes_sorted[0], eyes_sorted[1]
                lx = e1[0] + e1[2] / 2.0
                ly = e1[1] + e1[3] / 2.0
                rx = e2[0] + e2[2] / 2.0
                ry = e2[1] + e2[3] / 2.0

                if rx - lx > fw * 0.15:
                    roll_deg = math.degrees(math.atan2(ry - ly, rx - lx))
                    eye_mid_x = (lx + rx) / 2.0
                    eye_mid_y = (ly + ry) / 2.0
                    # Yuz markaziga nisbatan ko'zlar simmetriyasi -> Yaw
                    yaw_deg = ((eye_mid_x - (fw / 2.0)) / (fw * 0.25)) * 25.0
                    # Ko'zlarning vertikal joylashuvi (me'yor: 0.36 * fh) -> Pitch
                    pitch_deg = (((eye_mid_y / float(fh)) - 0.36) / 0.15) * 20.0
            else:
                # Agar ikkala ko'z aniq ko'rinmasa, chap va o'ng yarim yuz simmetriyasini tekshirish
                face_crop = gray_arr[fy:fy + fh, fx:fx + fw]
                half_w = fw // 2
                if half_w > 10:
                    left_half = face_crop[:, :half_w].astype(np.float32)
                    right_half_flipped = np.fliplr(face_crop[:, fw - half_w:]).astype(np.float32)
                    diff_mean = float(np.mean(np.abs(left_half - right_half_flipped)))
                    if diff_mean > 42.0:
                        yaw_deg = min(40.0, (diff_mean - 25.0) * 1.1)

            result['angles'] = {
                'yaw': round(yaw_deg, 1),
                'pitch': round(pitch_deg, 1),
                'roll': round(roll_deg, 1),
            }
            return result
        except Exception:
            pass

    return result


def validate_face_photo(
    image_source,
    filename: str | None = None,
    landmarks=None,
    pose_angles=None,
    min_sharpness: float = DEFAULT_MIN_SHARPNESS,
    max_yaw: float = MAX_YAW_ANGLE,
    max_pitch: float = MAX_PITCH_ANGLE,
    max_roll: float = MAX_ROLL_ANGLE,
    require_face: bool = False,
) -> dict:
    """
    Face ID uchun yuklanayotgan rasmni kompleks tekshiradi:
      1. Rasm formati (.jpg, .jpeg, .png) va minimal o'lchami;
      2. Rasm yorug'ligi va tiniqligi (sharpness / blur detection — Laplacian variance);
      3. Yuzning mavjudligi va kamera obyektiviga to'g'ri qarab turganligi (yaw, pitch, roll burchaklari).

    Qaytaradi:
      {
          "valid": bool,
          "error_type": str | None,
          "message": str,
          "sharpness": float,
          "brightness": float,
          "angles": {"yaw": float, "pitch": float, "roll": float},
          "format": str,
      }
    """
    # 1. Format va o'lcham tekshiruvi
    is_fmt_ok, pil_img, fmt_name, fmt_err = validate_image_format(image_source, filename=filename)
    if not is_fmt_ok or pil_img is None:
        return {
            'valid': False,
            'error_type': 'invalid_format',
            'message': fmt_err or "Noto'g'ri rasm formati! Faqat .jpg, .jpeg yoki .png rasm yuklang.",
            'sharpness': 0.0,
            'brightness': 0.0,
            'angles': {'yaw': 0.0, 'pitch': 0.0, 'roll': 0.0},
            'format': fmt_name,
        }

    # 2. Yuz va bosh burchaklarini (Pose / Angle) aniqlash
    pose_info = detect_face_and_pose(pil_img, landmarks=landmarks, pose_angles=pose_angles)
    face_box = pose_info.get('face_box')
    angles = pose_info.get('angles', {'yaw': 0.0, 'pitch': 0.0, 'roll': 0.0})

    # 3. Yorug'lik (Brightness) va Tiniqlik (Sharpness / Blur) tekshiruvi
    gray_full = pil_img.convert('L')
    brightness = round(float(ImageStat.Stat(gray_full).mean[0]), 1)
    sharpness = calculate_image_sharpness(pil_img, face_box=face_box)

    if brightness < 22.0:
        return {
            'valid': False,
            'error_type': 'too_dark',
            'message': "Rasm juda qorong'i! Iltimos, yorug'roq joyda yuzingiz aniq ko'rinadigan qilib suratga tushing.",
            'sharpness': sharpness,
            'brightness': brightness,
            'angles': angles,
            'pose': angles,
            'format': fmt_name,
        }

    if brightness > 250.0:
        return {
            'valid': False,
            'error_type': 'overexposed',
            'message': "Rasm haddan tashqari yorug' (oqarib qolgan)! Iltimos, yorug'likni me'yorlashtirib qayta suratga oling.",
            'sharpness': sharpness,
            'brightness': brightness,
            'angles': angles,
            'pose': angles,
            'format': fmt_name,
        }

    if sharpness < min_sharpness:
        return {
            'valid': False,
            'error_type': 'blurry_image',
            'message': (
                f"Rasm xira yoki yetarlicha tiniq emas (aniqlik darajasi: {sharpness:.1f}, "
                f"minimal talab: {min_sharpness:.0f}). Iltimos, kamerani qimirlatmasdan tiniq rasm yuklang!"
            ),
            'sharpness': sharpness,
            'brightness': brightness,
            'angles': angles,
            'pose': angles,
            'format': fmt_name,
        }

    # 4. Yuz mavjudligini tekshirish
    if (require_face or HAS_OPENCV or HAS_FACE_RECOGNITION) and not pose_info.get('face_detected', True):
        return {
            'valid': False,
            'error_type': 'no_face',
            'message': "Rasmda yuz aniqlanmadi! Iltimos, kamera obyektiviga to'g'ri qarab tushgan, yuz to'liq ko'ringan rasm yuklang.",
            'sharpness': sharpness,
            'brightness': brightness,
            'angles': angles,
            'pose': angles,
            'format': fmt_name,
        }

    if pose_info.get('face_count', 1) > 1:
        return {
            'valid': False,
            'error_type': 'multiple_faces',
            'message': f"Rasmda {pose_info['face_count']} ta yuz aniqlandi! Face ID uchun rasmda faqat bitta shaxs bo'lishi shart.",
            'sharpness': sharpness,
            'brightness': brightness,
            'angles': angles,
            'pose': angles,
            'format': fmt_name,
        }

    # 5. To'g'ri qarash (Yaw, Pitch, Roll burchaklari) tekshiruvi
    yaw = float(angles.get('yaw', 0.0))
    pitch = float(angles.get('pitch', 0.0))
    roll = float(angles.get('roll', 0.0))

    if abs(yaw) > max_yaw:
        side_str = "o'ngga" if yaw > 0 else "chapga"
        return {
            'valid': False,
            'error_type': 'bad_pose_yaw',
            'message': (
                f"Rasmga tushgan shaxs boshi yon tomonga ({side_str}) burilgan "
                f"(Yaw burchagi: {abs(yaw):.1f}°, ruxsat etilgan: ±{max_yaw:.0f}°). "
                "Iltimos, to'g'ridan-to'g'ri kamera obyektiviga qarab turing!"
            ),
            'sharpness': sharpness,
            'brightness': brightness,
            'angles': angles,
            'pose': angles,
            'format': fmt_name,
        }

    if abs(pitch) > max_pitch:
        vert_str = "pastga" if pitch > 0 else "tepaga"
        return {
            'valid': False,
            'error_type': 'bad_pose_pitch',
            'message': (
                f"Rasmga tushgan shaxs boshi {vert_str} egilgan "
                f"(Pitch burchagi: {abs(pitch):.1f}°, ruxsat etilgan: ±{max_pitch:.0f}°). "
                "Iltimos, boshingizni tik tutib, to'g'ri kameraga qarang!"
            ),
            'sharpness': sharpness,
            'brightness': brightness,
            'angles': angles,
            'pose': angles,
            'format': fmt_name,
        }

    if abs(roll) > max_roll:
        return {
            'valid': False,
            'error_type': 'bad_pose_roll',
            'message': (
                f"Rasmga tushgan shaxs boshi yonboshga qiyshaygan "
                f"(Roll burchagi: {abs(roll):.1f}°, ruxsat etilgan: ±{max_roll:.0f}°). "
                "Iltimos, boshingizni tekis tutib suratga tushing!"
            ),
            'sharpness': sharpness,
            'brightness': brightness,
            'angles': angles,
            'pose': angles,
            'format': fmt_name,
        }

    return {
        'valid': True,
        'error_type': None,
        'message': "Rasm barcha Face ID talablariga javob beradi (formati, tiniqligi va qarash burchagi to'g'ri) ✅",
        'sharpness': sharpness,
        'brightness': brightness,
        'angles': angles,
        'pose': angles,
        'format': fmt_name,
    }


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


def match_staff_by_face(
    scan_source=None,
    scan_descriptor=None,
    threshold: float = FACE_API_EUCLIDEAN_THRESHOLD,
    min_score: float = MIN_MATCH_SIMILARITY_SCORE,
):
    """
    Frontend (face-api.js) dan kelgan 128-o'lchamli `scan_descriptor` massivini
    bazadagi barcha faol xodimlarning (`Teacher` va `Manager`) saqlangan `face_embedding`
    descriptorlari bilan qat'iy Evklid masofasi (Euclidean distance) bo'yicha solishtiradi.

    Qat'iy xavfsizlik qoidalari:
      1. Masofa qat'iy threshold (<= 0.45) bo'lishi shart.
      2. O'xshashlik foizi kamida min_score (>= 72.0%) bo'lishi shart.
      3. Ambiguity Guard (Ikkilanuvchanlik himoyasi): agar 1-o'rindagi va 2-o'rindagi
         xodimlar orasidagi masofa farqi juda kichik (< 0.040) bo'lsa va 1-masofa
         yetarlicha yaqin (< 0.36) bo'lmasa, boshqa xodim bilan adashmaslik uchun rad etiladi.

    Eng ishonchli xodimni qaytaradi:
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

    matched_list = []

    for role, staff in candidates:
        staff_vec = parse_descriptor(staff.face_embedding)
        if staff_vec is None:
            continue

        score, is_match, dist = compare_embeddings(staff_vec, target_vec, threshold=threshold)
        if is_match and dist <= threshold and score >= min_score:
            matched_list.append({
                'role': role,
                'staff': staff,
                'score': score,
                'dist': dist,
            })

    if not matched_list:
        return None

    # Masofa bo'yicha eng kichikdan kattasiga tartiblash
    matched_list.sort(key=lambda item: item['dist'])
    best = matched_list[0]

    # Ambiguity Guard: ikkita turli xodim deyarli bir xil masofada mos kelib qolsa
    if len(matched_list) > 1:
        second = matched_list[1]
        # Agar 1-xodim va 2-xodim ID lari har xil bo'lsa
        if getattr(best['staff'], 'id', None) != getattr(second['staff'], 'id', None):
            gap = second['dist'] - best['dist']
            if gap < AMBIGUITY_DISTANCE_MARGIN and best['dist'] > 0.36:
                # Masofa farqi juda kam va 1-masofa mutlaq yaqin emas -> adashish xavfi yuqori
                return None

    return (best['role'], best['staff'], best['score'], best['dist'])


# ─────────────────────────────────────────────────────────────
# 4. DAVOMATNI YOZISH VA JARIMA HISOBLASH
# ─────────────────────────────────────────────────────────────

def calculate_late_fine_percent(late_minutes: int) -> Decimal:
    """
    Kechikish jarima foizini hisoblaydi:
    - 30 daqiqagacha bo'lgan kechikish (Grace Period) uchun: 0% jarima (hech qanday jarima hisoblanmaydi).
    - 30 daqiqadan oshgan har bir daqiqa uchun o'qituvchining oyligidan 1% miqdorida jarima foizi hisoblanadi.
      Formula: (late_minutes - 30) * 1.00%
    """
    if late_minutes <= 30:
        return Decimal('0.00')
    extra_minutes = late_minutes - 30
    return round(Decimal(str(extra_minutes)) * Decimal('1.00'), 2)


def calculate_teacher_penalty(salary: Decimal | float | int, late_minutes: int) -> tuple[Decimal, Decimal, bool]:
    """
    O'qituvchining maoshi va kechikish daqiqalari bo'yicha jarima foizi va summasini hisoblaydi:
    - 30 daqiqagacha kechikish: 0 jarima (0%, 0 so'm);
    - 30 daqiqadan oshgan har bir minut uchun: 1% jarima.
    Qaytaradi: (fine_percent, fine_amount, is_penalty_applied)
    """
    salary_dec = Decimal(str(salary or 0))
    fine_percent = calculate_late_fine_percent(late_minutes)
    if fine_percent > 0:
        fine_amount = round((salary_dec * fine_percent) / Decimal('100.0'), 2)
        return fine_percent, fine_amount, True
    return Decimal('0.00'), Decimal('0.00'), False


CHECKIN_ACTIONS = {'checkin', 'check_in', 'check-in', 'kelish', 'in'}
CHECKOUT_ACTIONS = {'checkout', 'check_out', 'check-out', 'ketish', 'out'}
CHECKOUT_DUPLICATE_WINDOW_SECONDS = 60


def normalize_scan_action(action) -> str | None:
    """Frontend'dan kelgan amalni 'checkin' / 'checkout' / None (avtomatik rejim) ga keltiradi."""
    if not action:
        return None
    val = str(action).strip().lower()
    if val in CHECKIN_ACTIONS:
        return 'checkin'
    if val in CHECKOUT_ACTIONS:
        return 'checkout'
    return None


def _coerce_time(value, default: time) -> time:
    """time obyekti yoki 'HH:MM[:SS]' satrini datetime.time ga o'giradi (model default qiymatlari satr bo'lishi mumkin)."""
    if isinstance(value, time):
        return value
    if isinstance(value, str) and value.strip():
        for fmt in ('%H:%M:%S', '%H:%M'):
            try:
                return datetime.strptime(value.strip(), fmt).time()
            except ValueError:
                continue
    return default


def get_staff_schedule_for_date(staff, target_date):
    """
    Xodimning berilgan sana uchun shaxsiy ish grafigini (kelish va ketish vaqti) qaytaradi.
    Agar o'qituvchida TeacherSchedule kiritilgan bo'lsa, aynan o'sha hafta kungi jadval olinadi.
    Aks holda xodimning standart work_start_time va work_end_time si olinadi.
    """
    if hasattr(staff, 'get_schedule_for_date'):
        return staff.get_schedule_for_date(target_date)
    return {
        'work_start_time': _coerce_time(getattr(staff, 'work_start_time', None), time(8, 0)),
        'work_end_time': _coerce_time(getattr(staff, 'work_end_time', None), time(18, 0)),
        'is_working_day': True,
        'is_custom': False,
        'schedule_obj': None,
        'note': '',
    }


def _apply_late_check_in(log: FaceIDLog, staff, now, today):
    """Kelish vaqtini yozadi va xodimning aynan bugungi kunlik shaxsiy ish grafigiga nisbatan kechikish/jarimani hisoblaydi."""
    sched_info = get_staff_schedule_for_date(staff, today)
    expected_time = _coerce_time(sched_info.get('work_start_time'), time(8, 0))
    work_end_time = _coerce_time(sched_info.get('work_end_time'), time(18, 0))
    is_working_day = sched_info.get('is_working_day', True)

    log.check_in_time = now

    # Agar bugun xodimning shaxsiy grafigi bo'yicha dam olish kuni bo'lsa
    if not is_working_day:
        log.is_late = False
        log.late_minutes = 0
        log.fine_percent_applied = Decimal('0.00')
        log.fine_amount_deducted = Decimal('0.00')
        log.status = 'on_time'
        if not log.notes:
            log.notes = "Dam olish kuni (rejadan tashqari keldi)"
        return

    expected_dt = datetime.combine(today, expected_time)
    expected_dt = timezone.make_aware(expected_dt) if timezone.is_aware(now) else expected_dt
    end_dt = datetime.combine(today, work_end_time)
    end_dt = timezone.make_aware(end_dt) if timezone.is_aware(now) else end_dt

    diff_seconds = (now - expected_dt).total_seconds()
    salary = Decimal(str(getattr(staff, 'monthly_salary', 0) or 0))

    if diff_seconds > 0 and now <= end_dt:
        late_mins = int(diff_seconds // 60) + (1 if (diff_seconds % 60) > 0 else 0)
        log.late_minutes = late_mins

        if late_mins <= 30:
            # ── 30 daqiqagacha bo'lgan kechikish: Imtiyozli vaqt (Grace Period) — 0 jarima ──
            log.is_late = False
            log.fine_percent_applied = Decimal('0.00')
            log.fine_amount_deducted = Decimal('0.00')
            log.status = 'on_time'
            grace_note = f"30 daqiqalik imtiyoz doirasida ({late_mins} daqiqa — jarimasiz)"
            if not log.notes:
                log.notes = grace_note
            elif 'imtiyoz' not in log.notes.lower():
                log.notes = f"{log.notes}; {grace_note}"
        else:
            # ── 30 daqiqadan oshgan qismi uchun: har bir daqiqa uchun 1% jarima ──
            fine_percent, fine_amount, _ = calculate_teacher_penalty(salary, late_mins)
            log.is_late = True
            log.fine_percent_applied = fine_percent
            log.fine_amount_deducted = fine_amount
            log.status = 'late'
            extra_mins = late_mins - 30
            penalty_note = f"Kechikish: {late_mins} daq (imtiyozdan tashqari {extra_mins} daq x 1% = {fine_percent}%)"
            if not log.notes:
                log.notes = penalty_note
            elif 'Kechikish:' not in log.notes:
                log.notes = f"{log.notes}; {penalty_note}"
    else:
        log.is_late = False
        log.late_minutes = 0
        log.fine_percent_applied = Decimal('0.00')
        log.fine_amount_deducted = Decimal('0.00')
        log.status = 'on_time'


def process_face_check_in(
    user: CustomUser,
    scan_file=None,
    lat=None,
    lon=None,
    bypass_geo=False,
    pre_verified: bool = True,
    match_score: float = 95.0,
    action=None,
):
    """
    Topilgan xodimning davomatini (FaceIDLog modeliga) avtomatik yozadi va JSON javob qaytaradi:
        {"success": True, "name": "Xodim ismi", "employee_name": "Xodim ismi", ...}

    `action` parametri:
      - 'checkin'  ("Keldim")  — faqat bugungi kelish vaqtini (check_in_time) yozadi;
      - 'checkout' ("Ketdim")  — faqat bugungi ketish vaqtini (check_out_time) yozadi;
      - None                   — eski avtomatik rejim (birinchi skan kelish, keyingisi ketish).
    """
    settings_obj = FaceIDSettings.get_settings()
    now = timezone.localtime()
    today = now.date()

    # Moslashuvchan parametrlar (Testlar yoki maxsus chaqiruvlarda sana/vaqt to'g'ridan-to'g'ri uzatilganda)
    if isinstance(scan_file, (date, datetime)):
        today = scan_file.date() if isinstance(scan_file, datetime) else scan_file
        if isinstance(lat, time):
            now = datetime.combine(today, lat)
            if timezone.is_aware(timezone.now()):
                now = timezone.make_aware(now)
        elif isinstance(scan_file, datetime):
            now = scan_file
        scan_file = None
        lat = None

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
    exemption = StaffAbsenceExemption.objects.filter(user=user, date=today).first()
    scan_action = normalize_scan_action(action)
    action_type = 'kelish'

    def _duplicate_response(dup_type: str, message: str) -> dict:
        return {
            'success': True,
            'name': employee_name,
            'employee_name': employee_name,
            'staff_name': employee_name,
            'message': message,
            'role': role_title,
            'action_type': dup_type,
            'scan_action': scan_action,
            'is_duplicate': True,
            'check_in_time': log.check_in_time.strftime('%H:%M:%S') if (log and log.check_in_time) else None,
            'check_out_time': log.check_out_time.strftime('%H:%M:%S') if (log and log.check_out_time) else None,
            'is_late': log.is_late if log else False,
            'late_minutes': log.late_minutes if log else 0,
            'fine_percent': float(log.fine_percent_applied) if log else 0.0,
            'fine_amount': float(log.fine_amount_deducted) if log else 0.0,
            'match_score': match_score,
            'is_excused': bool(exemption or (log and log.status == 'excused')),
            'exemption_reason': exemption.reason if exemption else (log.notes if log else ''),
        }

    # ── MAXSUS HOLAT: XODIMGA SABABLI KELMASLIK (ISTISNO) BELGILANGAN BO'LSA ──
    if exemption or (log and log.status == 'excused'):
        if log is None:
            log = FaceIDLog(
                user=user,
                date=today,
                status='excused',
                is_late=False,
                late_minutes=0,
                fine_percent_applied=Decimal('0.00'),
                fine_amount_deducted=Decimal('0.00'),
                notes=f"Sababli: {exemption.reason if exemption else ''}"
            )
        else:
            log.status = 'excused'
            log.is_late = False
            log.late_minutes = 0
            log.fine_percent_applied = Decimal('0.00')
            log.fine_amount_deducted = Decimal('0.00')
            if exemption and not log.notes:
                log.notes = f"Sababli: {exemption.reason}"

        if scan_action == 'checkout':
            action_type = 'ketish'
            log.check_out_time = now
        else:
            action_type = 'kelish'
            if not log.check_in_time:
                log.check_in_time = now

    elif scan_action == 'checkout':
        # ── "KETDIM" rejimi: faqat check_out_time yoziladi ──
        if log is None or not log.check_in_time:
            return {
                'success': False,
                'error_type': 'no_check_in',
                'name': employee_name,
                'employee_name': employee_name,
                'message': f"{employee_name}, bugun kelishingiz qayd etilmagan! Avval \"Keldim\" orqali kelishni belgilang.",
            }
        if log.check_out_time and (now - log.check_out_time).total_seconds() < CHECKOUT_DUPLICATE_WINDOW_SECONDS:
            return _duplicate_response('already_checked_out', "Xodim ketdi deb allaqachon belgilangan")
        action_type = 'ketish'
        log.check_out_time = now

    elif scan_action == 'checkin':
        # ── "KELDIM" rejimi: faqat check_in_time yoziladi (qayta skan ketish deb hisoblanmaydi) ──
        if log is not None and log.check_in_time:
            return _duplicate_response('already_checked_in', "Xodim keldi deb allaqachon belgilangan")
        if log is None:
            log = FaceIDLog(user=user, date=today)
        action_type = 'kelish'
        _apply_late_check_in(log, staff, now, today)

    elif log is None:
        # ── Avtomatik (eski) rejim: birinchi skan — kelish ──
        action_type = 'kelish'
        log = FaceIDLog(user=user, date=today)
        _apply_late_check_in(log, staff, now, today)

    elif log.check_in_time and not log.check_out_time:
        diff_since_in = (now - log.check_in_time).total_seconds()
        if diff_since_in < 5:
            return _duplicate_response('already_checked_in', "Xodim keldi deb belgilandi")
        action_type = 'ketish'
        log.check_out_time = now
    else:
        action_type = 'ketish'
        log.check_out_time = now

    # ── Shaxsiy kunlik jadvalni hisobga olish ──
    sched_info = get_staff_schedule_for_date(staff, today)
    sched_start_time = _coerce_time(sched_info.get('work_start_time'), time(8, 0))
    sched_end_time = _coerce_time(sched_info.get('work_end_time'), time(18, 0))
    is_sched_working = sched_info.get('is_working_day', True)

    sched_end_dt = datetime.combine(today, sched_end_time)
    sched_end_dt = timezone.make_aware(sched_end_dt) if timezone.is_aware(now) else sched_end_dt

    is_early_checkout = False
    early_leave_minutes = 0
    if action_type == 'ketish' and is_sched_working and now < sched_end_dt:
        diff_early = (sched_end_dt - now).total_seconds()
        early_leave_minutes = int(diff_early // 60) + (1 if (diff_early % 60) > 0 else 0)
        if early_leave_minutes > 0:
            is_early_checkout = True
            note_part = f"Erta ketish: {early_leave_minutes} daq (grafik: {sched_end_time.strftime('%H:%M')} gacha)"
            if not log.notes:
                log.notes = note_part
            elif 'Erta ketish' not in log.notes:
                log.notes = f"{log.notes}; {note_part}"

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

    if exemption or (log and log.status == 'excused'):
        ex_reason = exemption.reason if exemption else (log.notes.replace('Sababli:', '').strip() if log and log.notes else 'Ruxsat berilgan')
        status_msg = f"Sababli qayd etildi ({ex_reason}) ✅"
    elif action_type == 'kelish':
        if not is_sched_working:
            status_msg = f"{employee_name} keldi deb belgilandi (Dam olish kuni) ✅"
        elif log.is_late:
            is_teacher_emp = (getattr(user, 'role', '') == 'teacher' or role_title == "O'qituvchi")
            if is_teacher_emp:
                status_msg = f"{employee_name} keldi ({log.late_minutes} daqiqa kechikdi) ⚠️"
            else:
                extra_mins = max(0, log.late_minutes - 30)
                status_msg = f"{employee_name} keldi ({log.late_minutes} daqiqa kechikdi: 30 daqiqadan oshgan {extra_mins} daqiqa uchun {log.fine_percent_applied}% jarima) ⚠️"
        elif log.late_minutes > 0:
            status_msg = f"{employee_name} keldi ({log.late_minutes} daqiqa — 30 daqiqalik imtiyoz doirasida, jarimasiz) ✅"
        else:
            status_msg = f"{employee_name} o'z vaqtida keldi deb belgilandi (Grafik: {sched_start_time.strftime('%H:%M')}) ✅"
    else:
        if is_early_checkout:
            status_msg = f"{employee_name} ketdi deb belgilandi ({early_leave_minutes} daqiqa erta ketdi, grafik: {sched_end_time.strftime('%H:%M')} gacha) ⚠️"
        else:
            status_msg = f"{employee_name} ketdi deb belgilandi (Grafik: {sched_end_time.strftime('%H:%M')}) 👋"

    return {
        'success': True,
        'name': employee_name,
        'employee_name': employee_name,
        'staff_name': employee_name,
        'message': status_msg,
        'role': role_title,
        'action_type': action_type,
        'scan_action': scan_action,
        'is_excused': bool(exemption or (log and log.status == 'excused')),
        'exemption_reason': exemption.reason if exemption else (log.notes if log else ''),
        'check_in_time': log.check_in_time.strftime('%H:%M:%S') if log.check_in_time else None,
        'check_out_time': log.check_out_time.strftime('%H:%M:%S') if log.check_out_time else None,
        'is_late': log.is_late,
        'late_minutes': log.late_minutes,
        'fine_percent': float(log.fine_percent_applied),
        'fine_amount': float(log.fine_amount_deducted),
        'work_start_time': sched_start_time.strftime('%H:%M'),
        'work_end_time': sched_end_time.strftime('%H:%M'),
        'is_working_day': is_sched_working,
        'is_early_checkout': is_early_checkout,
        'early_minutes': early_leave_minutes,
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
