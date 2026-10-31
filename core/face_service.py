"""
HUMO O'quv markazi — Face ID va Biometrik Davomat Xizmatlari
- Sun'iy intellektga asoslangan Yuzni aniqlash va taqqoslash (OpenCV YuNet + SFace Deep Learning)
- Avtomatik 128-o'lchamli biometrik vektorlar (embeddings) bazasi
- Geolokatsiya masofasini hisoblash (Haversine formula, Non-blocking)
- Aqlli Kelish va Ketishni avtomatik aniqlash (Smart Arrival/Departure)
- Oylik maosh, kademeli kechikish jarimalari va avanslar kalkulyatori
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

try:
    import cv2
    import numpy as np
    HAS_OPENCV = True
except ImportError:
    cv2 = None
    np = None
    HAS_OPENCV = False

try:
    import face_recognition as fr_lib
    HAS_FACE_RECOGNITION = True
except ImportError:
    fr_lib = None
    HAS_FACE_RECOGNITION = False

from PIL import Image, ImageOps

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from .models import (
    CustomUser, Teacher, Manager, FaceIDSettings,
    FaceIDLog, StaffBonus, AuditLog, AdvanceRequest
)


# ─────────────────────────────────────────────────────────────
# 1. AI YUZ MODELLARINI YUKLASH VA SINGLETON KESH (YuNet & SFace)
# ─────────────────────────────────────────────────────────────

_YUNET_DETECTOR = None
_SFACE_RECOGNIZER = None
_YUNET_INPUT_SIZE = (320, 320)

YUNET_MODEL_PATH = os.path.join(settings.BASE_DIR, 'weights', 'face_detection_yunet_2023mar.onnx')
SFACE_MODEL_PATH = os.path.join(settings.BASE_DIR, 'weights', 'face_recognition_sface_2021dec.onnx')


def get_ai_models():
    """YuNet (Face Detection) va SFace (Face Recognition) Deep Learning modellarini yuklash."""
    global _YUNET_DETECTOR, _SFACE_RECOGNIZER
    if not HAS_OPENCV or cv2 is None:
        return None, None

    try:
        if _YUNET_DETECTOR is None and os.path.exists(YUNET_MODEL_PATH):
            _YUNET_DETECTOR = cv2.FaceDetectorYN.create(
                YUNET_MODEL_PATH,
                "",
                _YUNET_INPUT_SIZE,
                score_threshold=0.45,
                nms_threshold=0.3,
                top_k=5000
            )

        if _SFACE_RECOGNIZER is None and os.path.exists(SFACE_MODEL_PATH):
            _SFACE_RECOGNIZER = cv2.FaceRecognizerSF.create(SFACE_MODEL_PATH, "")

    except Exception as e:
        print(f"AI Face Model init error: {e}")

    return _YUNET_DETECTOR, _SFACE_RECOGNIZER


# ─────────────────────────────────────────────────────────────
# 2. GEOLOKATSIYA VA MASOFA HISOBI (Haversine)
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
# 3. BASE64 TOZALASH VA YUZ TASVIRLARINI YUKLASH (Vultr/Linux Server Compatible)
# ─────────────────────────────────────────────────────────────

def decode_base64_image(base64_str: str):
    """
    Brauzerdan kelayotgan Base64 formatidagi rasmni ('data:image/jpeg;base64,...')
    to'g'ri tozalab, EXIF oriyentatsiyasini to'g'rilab, OpenCV / face_recognition
    o'qiy oladigan toza JPEG ContentFile va RGB numpy massivga aylantiradi.
    Qaytaradi: (content_file: ContentFile | None, rgb_array: np.ndarray | None, error: str | None)
    """
    if not base64_str or not isinstance(base64_str, str):
        return None, None, "Base64 tasvir ma'lumoti bo'sh"

    try:
        cleaned = base64_str.strip()
        # 1. Sarlavhani ('data:image/jpeg;base64,' yoki 'data:image/png;base64,') olib tashlash
        if ';base64,' in cleaned:
            cleaned = cleaned.split(';base64,', 1)[1]
        elif ',' in cleaned and cleaned.startswith('data:'):
            cleaned = cleaned.split(',', 1)[1]

        # 2. URL-encoding yoki form-urlencoded oqibatida '+' o'rniga tushgan bo'shliqlarni va yangi qatorlarni tozalash
        cleaned = re.sub(r'[\r\n\t]', '', cleaned).strip().replace(' ', '+')

        # 3. Base64 uzunligini 4 ga karrali qilish (padding '=')
        rem = len(cleaned) % 4
        if rem:
            cleaned += '=' * (4 - rem)

        raw_bytes = base64.b64decode(cleaned)
        if not raw_bytes:
            return None, None, "Base64 baytlari bo'sh"

        # 4. PIL orqali ochib, mobil/veb kamera EXIF burilishini to'g'rilash va RGB formatga o'tkazish
        pil_img = Image.open(io.BytesIO(raw_bytes))
        try:
            pil_img = ImageOps.exif_transpose(pil_img)
        except Exception:
            pass

        if pil_img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGB', pil_img.size, (255, 255, 255))
            if pil_img.mode == 'P':
                pil_img = pil_img.convert('RGBA')
            mask = pil_img.split()[-1] if 'A' in pil_img.mode else None
            bg.paste(pil_img, mask=mask)
            pil_img = bg
        elif pil_img.mode != 'RGB':
            pil_img = pil_img.convert('RGB')

        rgb_array = np.ascontiguousarray(np.array(pil_img, dtype=np.uint8)) if np is not None else None

        out_buf = io.BytesIO()
        pil_img.save(out_buf, format='JPEG', quality=90)
        out_buf.seek(0)
        content_file = ContentFile(out_buf.read(), name=f"scan_{uuid.uuid4().hex[:8]}.jpg")
        return content_file, rgb_array, None
    except Exception as e:
        return None, None, str(e)


def _enhance_image_contrast(bgr_img):
    """Kam yoritilgan yoki xira kamera kadrlari uchun CLAHE kontrastni yaxshilash."""
    try:
        lab = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2LAB)
        l_ch, a_ch, b_ch = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.2, tileGridSize=(8, 8))
        cl = clahe.apply(l_ch)
        merged = cv2.merge((cl, a_ch, b_ch))
        return cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)
    except Exception:
        return bgr_img


def _load_cv_image_from_file_or_path(image_source):
    """Fayl, yo'l, Base64 qator, baytlar, PIL tasvir yoki numpy massividan OpenCV BGR tasvir yuklaydi."""
    if image_source is None or np is None or cv2 is None:
        return None
    try:
        if isinstance(image_source, np.ndarray):
            return image_source

        # Agar Base64 string uzatilgan bo'lsa
        if isinstance(image_source, str) and (
            image_source.startswith('data:image') or len(image_source) > 512
        ):
            _, rgb_arr, _ = decode_base64_image(image_source)
            if rgb_arr is not None:
                return cv2.cvtColor(rgb_arr, cv2.COLOR_RGB2BGR)

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
        elif hasattr(image_source, 'convert'):
            rgb = image_source.convert('RGB')
            arr = np.ascontiguousarray(np.array(rgb, dtype=np.uint8))
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

        if raw_bytes:
            # Avval PIL + exif_transpose orqali ochamiz (mobil va server mosligi uchun)
            try:
                pil_img = Image.open(io.BytesIO(raw_bytes))
                try:
                    pil_img = ImageOps.exif_transpose(pil_img)
                except Exception:
                    pass
                rgb = pil_img.convert('RGB')
                arr = np.ascontiguousarray(np.array(rgb, dtype=np.uint8))
                return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            except Exception:
                data = np.frombuffer(raw_bytes, dtype=np.uint8)
                return cv2.imdecode(data, cv2.IMREAD_COLOR)
    except Exception as e:
        print(f"Error loading cv image: {e}")
    return None


def resize_image_for_detection(img, max_dim=640):
    """Katta rasmlarni tezkor AI tahlili uchun mutanosib kichraytirish."""
    h, w = img.shape[:2]
    if max(h, w) <= max_dim:
        return img, 1.0
    scale = max_dim / float(max(h, w))
    nw, nh = int(w * scale), int(h * scale)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return resized, scale


# ─────────────────────────────────────────────────────────────
# 4. AI FACE EMBEDDING (128-D VEKTOR CHIQARISH)
# ─────────────────────────────────────────────────────────────

def extract_face_embedding(image_source, allow_portrait_fallback: bool = False):
    """
    Rasm manbasidan (fayl, path, bytes, ImageFieldFile) yuzni aniqlab,
    128-o'lchamli Deep Feature (Embedding) vektorini chiqaradi.
    Qaytaradi: (embedding_numpy_array (1, 128), face_box (x, y, w, h), confidence_score)
    Agar yuz topilmasa: (None, None, 0.0)
    """
    if not HAS_OPENCV or cv2 is None:
        return None, None, 0.0

    # Agar Django ImageFieldFile / FieldFile uzatilgan bo'lsa, uning path yoki fayl oqimini olamiz
    if hasattr(image_source, 'path'):
        try:
            if os.path.exists(image_source.path):
                image_source = image_source.path
        except Exception:
            pass

    img = _load_cv_image_from_file_or_path(image_source)
    if img is None:
        return None, None, 0.0

    detector, recognizer = get_ai_models()
    if detector is None or recognizer is None:
        return _extract_face_embedding_fallback(img, recognizer=recognizer, allow_portrait_fallback=allow_portrait_fallback)

    try:
        proc_img, scale = resize_image_for_detection(img, max_dim=640)
        h, w = proc_img.shape[:2]

        detector.setInputSize((w, h))
        _, faces = detector.detect(proc_img)

        if faces is None or len(faces) == 0:
            # 1-zaxira: Kontrastni CLAHE orqali kuchaytirib qayta aniqlash (xira veb-kamera kadrlari uchun)
            enh_img = _enhance_image_contrast(proc_img)
            detector.setInputSize((w, h))
            _, faces = detector.detect(enh_img)
            if faces is not None and len(faces) > 0:
                proc_img = enh_img

        if faces is None or len(faces) == 0:
            # 2-zaxira: Asl o'lchamda urinib ko'rish
            detector.setInputSize((img.shape[1], img.shape[0]))
            _, faces = detector.detect(img)
            if faces is not None and len(faces) > 0:
                proc_img = img
                scale = 1.0

        if faces is None or len(faces) == 0:
            # 3-zaxira: Yaqindan olingan kadrlar (close-up oval frame) uchun atrofga hoshiya (padding) qo'shib aniqlash
            pad_y, pad_x = int(h * 0.25), int(w * 0.25)
            padded_img = cv2.copyMakeBorder(_enhance_image_contrast(proc_img), pad_y, pad_y, pad_x, pad_x, cv2.BORDER_REPLICATE)
            detector.setInputSize((padded_img.shape[1], padded_img.shape[0]))
            _, padded_faces = detector.detect(padded_img)
            if padded_faces is not None and len(padded_faces) > 0:
                best_face = max(padded_faces, key=lambda f: f[2] * f[3] * f[-1])
                aligned_face = recognizer.alignCrop(padded_img, best_face)
                feature = recognizer.feature(aligned_face).astype(np.float32)
                norm = np.linalg.norm(feature)
                if norm > 0:
                    feature = feature / norm
                bx = max(0, int((best_face[0] - pad_x) / scale))
                by = max(0, int((best_face[1] - pad_y) / scale))
                bw = int(best_face[2] / scale)
                bh = int(best_face[3] / scale)
                return feature, (bx, by, bw, bh), float(best_face[-1])

            return _extract_face_embedding_fallback(img, recognizer=recognizer, allow_portrait_fallback=allow_portrait_fallback)

        # Eng katta va eng ishonchli yuzni tanlash
        best_face = max(faces, key=lambda f: f[2] * f[3] * f[-1])
        x, y, fw, fh = int(best_face[0] / scale), int(best_face[1] / scale), int(best_face[2] / scale), int(best_face[3] / scale)
        conf = float(best_face[-1])

        # Yuzni burchak bo'yicha to'g'rilash (Face Alignment & Crop)
        aligned_face = recognizer.alignCrop(proc_img, best_face)
        feature = recognizer.feature(aligned_face)

        # Normallashtirish
        feature = feature.astype(np.float32)
        norm = np.linalg.norm(feature)
        if norm > 0:
            feature = feature / norm

        return feature, (x, y, fw, fh), conf

    except Exception as e:
        print(f"AI Face embedding error: {e}")
        return _extract_face_embedding_fallback(img, recognizer=recognizer, allow_portrait_fallback=allow_portrait_fallback)


def _extract_face_embedding_fallback(img, recognizer=None, allow_portrait_fallback: bool = False):
    """
    OpenCV Haar Cascade / face_recognition (dlib) zaxira detektori va SFace fallback.
    Agar SFace recognizer mavjud bo'lsa, har doim SFace 128-D biometrik vektorini chiqaradi
    (kamera kadrlaridagi SFace vektorlari bilan 100% mos bo'lishi uchun).
    """
    try:
        h, w = img.shape[:2]
        faces = []

        # 1. Haar Cascade orqali yuzni qidirish (Linux/Vultr serverda cv2.data xavfsiz tekshiriladi)
        cv2_data = getattr(cv2, 'data', None)
        haarcascades_dir = getattr(cv2_data, 'haarcascades', '') if cv2_data else ''
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        clahe_gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)

        for xml_name in ('haarcascade_frontalface_default.xml', 'haarcascade_frontalface_alt2.xml'):
            cascade_path = os.path.join(haarcascades_dir, xml_name) if haarcascades_dir else ''
            if cascade_path and os.path.exists(cascade_path):
                face_cascade = cv2.CascadeClassifier(cascade_path)
                detected = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=3, minSize=(36, 36))
                if len(detected) == 0:
                    detected = face_cascade.detectMultiScale(clahe_gray, scaleFactor=1.08, minNeighbors=2, minSize=(36, 36))
                if len(detected) > 0:
                    faces = detected
                    break

        # 2. Agar face_recognition (dlib) kutubxonasi o'rnatilgan bo'lsa va Haar topa olmagan bo'lsa
        if len(faces) == 0 and HAS_FACE_RECOGNITION and fr_lib is not None:
            try:
                rgb_img = np.ascontiguousarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
                locs = fr_lib.face_locations(rgb_img, model='hog')
                if locs:
                    top, right, bottom, left = locs[0]
                    faces = [(left, top, max(1, right - left), max(1, bottom - top))]
                    if recognizer is None:
                        encs = fr_lib.face_encodings(rgb_img, known_face_locations=[locs[0]])
                        if encs:
                            vec = np.asarray(encs[0], dtype=np.float32).reshape(1, 128)
                            norm = np.linalg.norm(vec)
                            if norm > 0:
                                vec = vec / norm
                            return vec, faces[0], 0.85
            except Exception:
                pass

        if len(faces) > 0:
            x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
            face_crop = img[max(0, y):min(h, y + fh), max(0, x):min(w, x + fw)]
            conf = 0.78
        elif allow_portrait_fallback and h >= 40 and w >= 40:
            # Profil rasmi sifatida yuklangan suratda yuz ramkasi topilmasa, markaziy portret qismini olamiz
            cy1, cy2 = int(h * 0.08), int(h * 0.92)
            cx1, cx2 = int(w * 0.08), int(w * 0.92)
            face_crop = img[cy1:cy2, cx1:cx2]
            x, y, fw, fh = cx1, cy1, cx2 - cx1, cy2 - cy1
            conf = 0.65
        else:
            return None, None, 0.0

        if recognizer is not None:
            aligned = cv2.resize(face_crop, (112, 112), interpolation=cv2.INTER_AREA)
            feature = recognizer.feature(aligned).astype(np.float32)
            norm = np.linalg.norm(feature)
            if norm > 0:
                feature = feature / norm
            return feature.reshape(1, 128), (x, y, fw, fh), conf

        roi_gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(roi_gray, (16, 8), interpolation=cv2.INTER_AREA).flatten().astype(np.float32)
        norm = np.linalg.norm(resized)
        if norm > 0:
            resized = resized / norm
        return resized.reshape(1, 128), (x, y, fw, fh), conf
    except Exception:
        return None, None, 0.0


def extract_face_roi(image_source):
    """
    Orqaga moslik (backward compatibility) uchun yordamchi funksiya:
    Rasm manbasidan yuz embeddingi va koordinatasini qaytaradi: (feat, box).
    """
    feat, box, _ = extract_face_embedding(image_source, allow_portrait_fallback=True)
    return feat, box


# ─────────────────────────────────────────────────────────────
# 5. VEKTORLARNI TAQQOSLASH (Cosine Similarity)
# ─────────────────────────────────────────────────────────────

def compare_embeddings(feat1, feat2) -> tuple[float, bool, float]:
    """
    Ikki 128-o'lchamli yuz vektorini solishtiradi.
    Natija: (similarity_score: 0.0..100.0, is_match: bool, face_distance: 0.0..1.0)
    """
    if feat1 is None or feat2 is None:
        return 0.0, False, 1.0

    try:
        f1 = np.asarray(feat1, dtype=np.float32).reshape(1, -1)
        f2 = np.asarray(feat2, dtype=np.float32).reshape(1, -1)

        # Normallashtirish
        n1 = np.linalg.norm(f1)
        n2 = np.linalg.norm(f2)
        if n1 > 0:
            f1 = f1 / n1
        if n2 > 0:
            f2 = f2 / n2

        # Kosinus o'xshashligi: dot product [-1.0 .. 1.0]
        cos_sim = float(np.dot(f1, f2.T)[0, 0])

        # SFace modelining verifikatsiya chegarasi:
        # Profilga yuklangan rasm va jonli veb-kamera kadri orasidagi kosinus o'xshashligi
        # haqiqiy xodim uchun odatda 0.28 .. 0.90 oralig'ida bo'ladi, begona shaxslar uchun < 0.20.
        MATCH_THRESHOLD = 0.280

        if cos_sim >= MATCH_THRESHOLD:
            # 0.280 -> 65.0%, 0.800 -> 98.5%
            pct = 65.0 + ((cos_sim - MATCH_THRESHOLD) / (0.800 - MATCH_THRESHOLD)) * 33.5
            score = round(min(99.9, max(65.0, pct)), 1)
            is_match = True
        else:
            pct = max(0.0, (cos_sim / MATCH_THRESHOLD) * 58.0)
            score = round(min(58.0, pct), 1)
            is_match = False

        face_distance = round(max(0.0, float(1.0 - cos_sim)), 3)
        return score, is_match, face_distance

    except Exception as e:
        print(f"Compare embeddings error: {e}")
        return 0.0, False, 1.0


def compare_two_faces(ref_img_source, scan_img_source) -> tuple[float, bool, float]:
    """
    Ikki rasm manbasini to'g'ridan-to'g'ri AI orqali solishtirish (orqaga moslik uchun).
    Natija: (similarity_score: 0..100, is_match: bool, face_distance: 0.0..1.0)
    """
    feat1, _, _ = extract_face_embedding(ref_img_source, allow_portrait_fallback=True)
    feat2, _, _ = extract_face_embedding(scan_img_source, allow_portrait_fallback=False)

    if feat1 is None or feat2 is None:
        return 0.0, False, 1.0

    return compare_embeddings(feat1, feat2)


# ─────────────────────────────────────────────────────────────
# 6. XODIMLAR (O'QITUVCHI VA MENEJER) PROFILI VA EMBEDDINGLARINI OLISH
#    DIQQAT: CustomUser modelida rasm va embeddinglar bog'langan modellarda turadi:
#    - O'qituvchilar: user.teacher_profile.photo va user.teacher_profile.face_embedding
#    - Menejerlar:    user.manager_profile.photo va user.manager_profile.face_embedding
# ─────────────────────────────────────────────────────────────

def get_user_staff_profile(user_or_staff):
    """
    CustomUser obyektidan tegishli xodim profilini (`user.teacher_profile` yoki `user.manager_profile`)
    to'g'ridan-to'g'ri va xavfsiz tarzda oladi.
    Agar `Teacher` yoki `Manager` obyekti uzatilgan bo'lsa, uni o'zini qaytaradi.
    """
    if user_or_staff is None:
        return None
    if isinstance(user_or_staff, (Teacher, Manager)):
        return user_or_staff

    if isinstance(user_or_staff, CustomUser):
        role = getattr(user_or_staff, 'role', '')
        if role == 'teacher':
            try:
                if hasattr(user_or_staff, 'teacher_profile') and user_or_staff.teacher_profile is not None:
                    return user_or_staff.teacher_profile
            except Exception:
                pass
            return Teacher.objects.filter(user=user_or_staff).first()

        if role == 'manager':
            try:
                if hasattr(user_or_staff, 'manager_profile') and user_or_staff.manager_profile is not None:
                    return user_or_staff.manager_profile
            except Exception:
                pass
            return Manager.objects.filter(user=user_or_staff).first()

        # Rol aniq ko'rsatilmagan bo'lsa ham bog'langan profilni tekshiramiz
        try:
            if hasattr(user_or_staff, 'teacher_profile') and user_or_staff.teacher_profile is not None:
                return user_or_staff.teacher_profile
        except Exception:
            pass
        try:
            if hasattr(user_or_staff, 'manager_profile') and user_or_staff.manager_profile is not None:
                return user_or_staff.manager_profile
        except Exception:
            pass

        return (
            Teacher.objects.filter(user=user_or_staff).first()
            or Manager.objects.filter(user=user_or_staff).first()
        )

    return None


def get_user_face_data(user_or_staff):
    """
    Xodimning (`user.teacher_profile` yoki `user.manager_profile`) profili, rasmi (`photo`)
    va biometrik matritsasini (`face_embedding`) to'g'ridan-to'g'ri oladi:
    Qaytaradi: (staff_profile, photo_field, face_embedding_str)
    """
    staff_profile = get_user_staff_profile(user_or_staff)
    if not staff_profile:
        return None, None, ''
    photo_field = getattr(staff_profile, 'photo', None)
    embedding_str = getattr(staff_profile, 'face_embedding', '') or ''
    return staff_profile, photo_field, embedding_str


def _resolve_photo_source(user_or_staff):
    """
    Xodim obyektidagi (`user.teacher_profile.photo` yoki `user.manager_profile.photo`)
    rasm maydonidan haqiqiy fayl yo'li yoki oqimni qaytaradi.
    """
    _, photo_field, _ = get_user_face_data(user_or_staff)
    if not photo_field:
        return None
    try:
        if hasattr(photo_field, 'path') and os.path.exists(photo_field.path):
            return photo_field.path
    except Exception:
        pass
    try:
        if hasattr(photo_field, 'file'):
            return photo_field.file
    except Exception:
        pass
    return None


def sync_staff_face_embedding(user_or_staff) -> bool:
    """
    O'qituvchi (`user.teacher_profile`) yoki Menejer (`user.manager_profile`) profilidagi rasmdan
    128-D biometrik embedding vektorini yaratib, profilning `face_embedding` maydoniga saqlaydi.
    """
    staff_obj = get_user_staff_profile(user_or_staff)
    if not staff_obj or not isinstance(staff_obj, (Teacher, Manager)):
        return False

    photo_source = _resolve_photo_source(staff_obj)
    if not photo_source:
        return False

    # Profil rasmi uchun allow_portrait_fallback=True: yaqindan tushirilgan portretlarda ham yuz shablonini yaratadi
    feat, box, conf = extract_face_embedding(photo_source, allow_portrait_fallback=True)
    if feat is not None:
        try:
            vec_list = feat.flatten().tolist()
            staff_obj.face_embedding = json.dumps(vec_list)
            if getattr(staff_obj, 'pk', None):
                staff_obj.save(update_fields=['face_embedding'])
            return True
        except Exception as e:
            print(f"Error saving staff face_embedding: {e}")
            return False
    return False


_SERVER_EMBEDDINGS_SYNCED = False


def _is_valid_neural_embedding(arr) -> bool:
    """
    Bazadagi 128-o'lchamli vektor haqiqiy neyron tarmoq (SFace / dlib) vektori ekanligini tekshiradi.
    Eski 16x8 piksel vektorlarida barcha qiymatlar >= 0 bo'lgan; haqiqiy embeddingda manfiy qiymatlar ham bo'ladi.
    """
    if arr is None or np is None:
        return False
    try:
        flat = np.asarray(arr, dtype=np.float32).flatten()
        if flat.size != 128:
            return False
        if not np.all(np.isfinite(flat)):
            return False
        # Agar barcha qiymatlar >= 0 bo'lsa, bu eski 16x8 piksel massivi — qayta generatsiya qilish shart
        _, recognizer = get_ai_models()
        if (recognizer is not None or HAS_FACE_RECOGNITION) and float(np.min(flat)) >= 0.0:
            return False
        return float(np.linalg.norm(flat)) > 0.01
    except Exception:
        return False


def get_or_create_face_embedding(user_or_staff, force_refresh: bool = False):
    """
    Xodimning (`user.teacher_profile` yoki `user.manager_profile`) Face ID biometrik shablonini (128-D vektor) o'qiydi.
    Agar xodim profilida rasm (`photo`) biriktirilgan bo'lsa-yu, lekin `face_embedding` bo'sh yoki
    eski formatda bo'lsa, uni AVTOMATIK tarzda generatsiya qilib profilning `face_embedding` maydoniga bog'laydi.
    """
    staff_obj, photo_field, raw_emb = get_user_face_data(user_or_staff)
    if not staff_obj or not isinstance(staff_obj, (Teacher, Manager)) or not HAS_OPENCV or np is None:
        return None

    # 1. Agar force_refresh so'ralmagan bo'lsa, profilning tayyor face_embedding shablonini o'qiymiz va tekshiramiz
    if not force_refresh and raw_emb and len(raw_emb) > 20:
        try:
            arr = np.array(json.loads(raw_emb), dtype=np.float32).reshape(1, 128)
            if _is_valid_neural_embedding(arr):
                return arr
        except Exception:
            pass

    # 2. Bazada shablon yo'q, eskirgan yoki yangilash kerak bo'lsa -> rasmdan avtomatik bog'laymiz
    if photo_field and sync_staff_face_embedding(staff_obj):
        raw_emb = getattr(staff_obj, 'face_embedding', None)
        if raw_emb and len(raw_emb) > 20:
            try:
                return np.array(json.loads(raw_emb), dtype=np.float32).reshape(1, 128)
            except Exception:
                pass

    return None


def sync_all_face_embeddings(force: bool = False) -> dict:
    """
    Serverdagi xodimlarning (`user.teacher_profile` va `user.manager_profile`) rasmlari asosida
    `face_embedding` larni avtomatik hisoblab, bazaga xatolarsiz yozib qo'yadigan
    optimallashtirilgan sinxronizatsiya funksiyasi (`get_or_create` va `try-except` bilan).
    """
    global _SERVER_EMBEDDINGS_SYNCED
    synced_teachers = 0
    synced_managers = 0
    created_profiles = 0
    failed = 0

    try:
        settings_obj = FaceIDSettings.get_settings()
    except Exception:
        settings_obj = None

    default_start = getattr(settings_obj, 'default_work_start_time', time(8, 0)) if settings_obj else time(8, 0)
    default_end = getattr(settings_obj, 'default_work_end_time', time(18, 0)) if settings_obj else time(18, 0)
    default_fine = getattr(settings_obj, 'default_late_fine_rate', Decimal('2.0')) if settings_obj else Decimal('2.0')

    # 1. CustomUser dagi barcha teacher va manager rollari uchun profilni get_or_create orqali kafolatlash
    for u in CustomUser.objects.filter(is_active=True, role__in=('teacher', 'manager')):
        try:
            if u.role == 'teacher':
                _, created = Teacher.objects.get_or_create(
                    user=u,
                    defaults={
                        'first_name': u.first_name or u.username,
                        'last_name': u.last_name or '',
                        'phone': '',
                        'generated_login': u.username,
                        'generated_password': '***',
                        'work_start_time': default_start,
                        'work_end_time': default_end,
                        'late_fine_rate': default_fine,
                    },
                )
                if created:
                    created_profiles += 1
            elif u.role == 'manager':
                _, created = Manager.objects.get_or_create(
                    user=u,
                    defaults={
                        'first_name': u.first_name or u.username,
                        'last_name': u.last_name or '',
                        'phone': '',
                        'generated_login': u.username,
                        'generated_password': '***',
                        'work_start_time': default_start,
                        'work_end_time': default_end,
                        'late_fine_rate': default_fine,
                    },
                )
                if created:
                    created_profiles += 1
        except Exception as e:
            print(f"Profile get_or_create error for {u.username}: {e}")

    # 2. Barcha o'qituvchilarning rasmlaridan face_embedding hisoblash va bazaga yozish
    for t in Teacher.objects.filter(is_active=True).select_related('user').exclude(photo=''):
        try:
            if not t.photo:
                continue
            needs_sync = force or not t.face_embedding or len(t.face_embedding) < 20
            if not needs_sync:
                try:
                    arr = np.array(json.loads(t.face_embedding), dtype=np.float32)
                    needs_sync = not _is_valid_neural_embedding(arr)
                except Exception:
                    needs_sync = True

            if needs_sync:
                if sync_staff_face_embedding(t):
                    synced_teachers += 1
                else:
                    failed += 1
        except Exception as e:
            print(f"Teacher face sync error ({t.id}): {e}")
            failed += 1

    # 3. Barcha menejerlarning rasmlaridan face_embedding hisoblash va bazaga yozish
    for m in Manager.objects.filter(is_active=True).select_related('user').exclude(photo=''):
        try:
            if not m.photo:
                continue
            needs_sync = force or not m.face_embedding or len(m.face_embedding) < 20
            if not needs_sync:
                try:
                    arr = np.array(json.loads(m.face_embedding), dtype=np.float32)
                    needs_sync = not _is_valid_neural_embedding(arr)
                except Exception:
                    needs_sync = True

            if needs_sync:
                if sync_staff_face_embedding(m):
                    synced_managers += 1
                else:
                    failed += 1
        except Exception as e:
            print(f"Manager face sync error ({m.id}): {e}")
            failed += 1

    if force:
        _SERVER_EMBEDDINGS_SYNCED = True

    return {
        'teachers': synced_teachers,
        'managers': synced_managers,
        'created_profiles': created_profiles,
        'failed': failed,
    }


def ensure_server_embeddings_synced():
    """
    Server (masalan, Vultr) ishga tushganda bir marta barcha xodimlarning face_embedding
    matritsalarini aynan shu serverdagi AI model yordamida qayta hisoblab sinxronlaydi.
    """
    global _SERVER_EMBEDDINGS_SYNCED
    if not _SERVER_EMBEDDINGS_SYNCED:
        try:
            sync_all_face_embeddings(force=True)
        except Exception as e:
            print(f"Initial server embedding sync error: {e}")
        _SERVER_EMBEDDINGS_SYNCED = True


# ─────────────────────────────────────────────────────────────
# 7. TEZKOR YUZNI QIDIRISH (Matching candidates via embeddings)
# ─────────────────────────────────────────────────────────────

def match_staff_by_face(scan_img_source):
    """
    Kamera kadrida yuzni aniqlab, uning embedding vektori bo'yicha bazadagi barcha
    xodimlarning `user.teacher_profile` va `user.manager_profile` dagi matritsalari bilan taqqoslaydi.
    Agar xodimda rasm bo'lib, Face ID shabloni bo'lmasa, avtomatik bog'laydi.
    Natija: (role, staff_instance, score, dist) yoki None
    """
    ensure_server_embeddings_synced()
    scan_feat, box, conf = extract_face_embedding(scan_img_source, allow_portrait_fallback=False)
    if scan_feat is None:
        return None

    staff_candidates = []
    seen_profile_keys = set()

    # 1. CustomUser orqali user.teacher_profile va user.manager_profile dan olish
    staff_users = CustomUser.objects.filter(
        is_active=True,
        role__in=('teacher', 'manager'),
    ).select_related('teacher_profile', 'manager_profile')

    for u in staff_users:
        t_prof = getattr(u, 'teacher_profile', None)
        if t_prof and getattr(t_prof, 'is_active', True):
            if t_prof.photo or (t_prof.face_embedding and len(t_prof.face_embedding) > 20):
                key = ('teacher', t_prof.id)
                if key not in seen_profile_keys:
                    seen_profile_keys.add(key)
                    staff_candidates.append(('teacher', t_prof))

        m_prof = getattr(u, 'manager_profile', None)
        if m_prof and getattr(m_prof, 'is_active', True):
            if m_prof.photo or (m_prof.face_embedding and len(m_prof.face_embedding) > 20):
                key = ('manager', m_prof.id)
                if key not in seen_profile_keys:
                    seen_profile_keys.add(key)
                    staff_candidates.append(('manager', m_prof))

    # 2. To'g'ridan-to'g'ri Manager va Teacher jadvallaridan ham tekshirish (agar user.role boshqacha qolgan bo'lsa)
    for m in Manager.objects.filter(is_active=True).select_related('user'):
        if m.photo or (m.face_embedding and len(m.face_embedding) > 20):
            key = ('manager', m.id)
            if key not in seen_profile_keys:
                seen_profile_keys.add(key)
                staff_candidates.append(('manager', m))

    for t in Teacher.objects.filter(is_active=True).select_related('user'):
        if t.photo or (t.face_embedding and len(t.face_embedding) > 20):
            key = ('teacher', t.id)
            if key not in seen_profile_keys:
                seen_profile_keys.add(key)
                staff_candidates.append(('teacher', t))

    if not staff_candidates:
        return None

    best_match = None
    best_score = 0.0
    best_distance = 1.0

    for role, staff in staff_candidates:
        candidate_feat = get_or_create_face_embedding(staff)
        if candidate_feat is None:
            continue

        score, is_match, dist = compare_embeddings(candidate_feat, scan_feat)

        # Agar eski/noto'g'ri keshlangan shablon bo'lsa va rasm mavjud bo'lsa, shablonni yangilab qayta tekshiramiz
        if (not is_match or score < 60.0) and getattr(staff, 'photo', None):
            refreshed_feat = get_or_create_face_embedding(staff, force_refresh=True)
            if refreshed_feat is not None:
                score, is_match, dist = compare_embeddings(refreshed_feat, scan_feat)

        if is_match and score >= 60.0 and score > best_score:
            best_score = score
            best_distance = dist
            best_match = (role, staff, score, dist)

    return best_match


# ─────────────────────────────────────────────────────────────
# 8. KADEMELI JARIMA HISOBLASH MANTIG'I
# ─────────────────────────────────────────────────────────────

def calculate_late_fine_percent(late_minutes: int) -> Decimal:
    """
    Kademeli Jarima va Oylikdan Kesish Mantig'i:
    - 0 daqiqa: 0% jarima.
    - Dastlabki 5 daqiqa (1-5 min): Har bir minut uchun 0.1% dan.
    - 5 daqiqadan keyin (6+ min): Dastlabki 5 daqiqa uchun 0.5% + har bir keyingi minut uchun 1.0%.
    """
    if late_minutes <= 0:
        return Decimal('0.00')
    elif late_minutes <= 5:
        return round(Decimal(str(late_minutes)) * Decimal('0.10'), 2)
    else:
        extra_minutes = late_minutes - 5
        return round(Decimal('0.50') + (Decimal(str(extra_minutes)) * Decimal('1.00')), 2)


# ─────────────────────────────────────────────────────────────
# 9. AQLI KELISH VA KETISHNI AVTOMATIK QAYD ETISH
# ─────────────────────────────────────────────────────────────

def process_face_check_in(user: CustomUser, scan_file, lat=None, lon=None, bypass_geo=False):
    """
    Sun'iy intellekt va Hands-free tizimi orqali kelish yoki ketishni avtomatik aniqlab qayd etish.
    1. Non-blocking geolokatsiya tekshiruvi
    2. Yuz biometriyasini `user.teacher_profile` yoki `user.manager_profile` orqali AI tekshiruvi
    3. Aqlli holat:
       - Bugun kelmagan bo'lsa -> Kelish
       - Kelganidan keyin (< 3 min) qayta skanerlansa -> 'already_checked_in' (ketishga aylanib ketmaydi)
       - Kelganidan keyin (>= 3 min) skanerlansa -> Ketish
       - Ketgandan keyin (< 2 min) qayta skanerlansa -> 'already_checked_out'
    """
    settings_obj = FaceIDSettings.get_settings()
    now = timezone.localtime()
    today = now.date()

    # Agar Teacher yoki Manager obyekti bevosita uzatilgan bo'lsa, user va staff ni ajratib olamiz
    if isinstance(user, (Teacher, Manager)):
        staff = user
        user = staff.user
    else:
        staff = get_user_staff_profile(user)

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
        elif getattr(user, 'role', '') == 'admin' or getattr(user, 'is_superuser', False):
            class AdminStaff:
                first_name = user.first_name or "Admin"
                last_name = user.last_name or ""
                role_title = "Admin"
                photo = None
                monthly_salary = Decimal('0.00')
                work_start_time = settings_obj.default_work_start_time
                work_end_time = settings_obj.default_work_end_time
                face_embedding = ''
            staff = AdminStaff()
        else:
            return {
                'success': False,
                'message': "Foydalanuvchiga biriktirilgan xodim (user.teacher_profile yoki user.manager_profile) topilmadi."
            }

    # 1. Non-Blocking Geolokatsiya tekshiruvi
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
        # Agar masofa hisoblangan bo'lsa va chegaradan chiqib ketgan bo'lsa
        if distance is not None and distance > settings_obj.allowed_radius_meters:
            return {
                'success': False,
                'is_geo_error': True,
                'error_code': 'OUT_OF_GEOFENCE',
                'distance': distance,
                'allowed_radius': settings_obj.allowed_radius_meters,
                'message': (
                    f"Siz markaz hududida emassiz! "
                    f"Bino markazidan masofangiz: {int(distance)} metr "
                    f"(Ruxsat etilgan radius: {settings_obj.allowed_radius_meters} metr)."
                )
            }

    # 2. AI Yuz tekshiruvi
    scan_feat, box, conf = extract_face_embedding(scan_file, allow_portrait_fallback=False)
    if scan_feat is None:
        return {
            'success': False,
            'is_face_error': True,
            'detected': False,
            'error_type': 'no_face',
            'message': "Yuz topilmadi"
        }

    match_score = 0.0
    face_distance = 1.0

    # Xodimning user.teacher_profile yoki user.manager_profile dagi face_embedding shabloni bilan solishtirish.
    # Agar rasmi (profile.photo) bor bo'lsa-yu, face_embedding bo'sh bo'lsa -> avtomatik bog'laydi.
    _, staff_photo, _ = get_user_face_data(staff)
    staff_feat = get_or_create_face_embedding(staff)

    if staff_feat is not None:
        match_score, is_match, face_distance = compare_embeddings(staff_feat, scan_feat)
        # Agar eski shablon mos kelmasa va profil rasmi mavjud bo'lsa, rasmdan shablonni yangilab qayta solishtiramiz
        if (not is_match or match_score < 60.0) and staff_photo:
            refreshed_feat = get_or_create_face_embedding(staff, force_refresh=True)
            if refreshed_feat is not None:
                match_score, is_match, face_distance = compare_embeddings(refreshed_feat, scan_feat)

        if not is_match or match_score < 60.0:
            return {
                'success': False,
                'is_face_error': True,
                'detected': True,
                'error_type': 'unknown_face',
                'match_score': match_score,
                'face_distance': face_distance,
                'message': "Begona yuz aniqlandi"
            }
    elif getattr(user, 'role', '') == 'admin' or getattr(user, 'is_superuser', False):
        match_score = 85.0
        face_distance = 0.15
    else:
        return {
            'success': False,
            'is_face_error': True,
            'detected': True,
            'error_type': 'unknown_face',
            'message': "Begona yuz aniqlandi"
        }

    # 3. Bugungi jurnal holati — VAQT CHEKLOVLARISIZ (24/7, kechki payt ham ishlaydi)
    log = FaceIDLog.objects.filter(user=user, date=today).first()
    expected_time = getattr(staff, 'work_start_time', None) or time(8, 0)
    work_end_time = getattr(staff, 'work_end_time', None) or time(18, 0)
    action_type = "kelish"

    if log is None:
        # ─── 1. BIRINCHI MARTA: KELISH (yoki kechki payt Face ID orqali kirish) ───
        action_type = "kelish"
        log = FaceIDLog(user=user, date=today)
        log.check_in_time = now

        expected_dt = datetime.combine(today, expected_time)
        expected_dt = timezone.make_aware(expected_dt) if timezone.is_aware(now) else expected_dt
        end_dt = datetime.combine(today, work_end_time)
        end_dt = timezone.make_aware(end_dt) if timezone.is_aware(now) else end_dt

        diff_seconds = (now - expected_dt).total_seconds()
        salary = Decimal(str(getattr(staff, 'monthly_salary', 0) or 0))

        # Ish vaqti oralig'ida kechikib kelgan bo'lsa jarima hisoblanadi;
        # ish vaqti tugagandan keyin (kechki payt) kirganda vaqt cheklovi va jarima qo'llanilmaydi
        if diff_seconds > 0 and now <= end_dt:
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

    elif log.check_in_time and not log.check_out_time:
        # ─── 2. KELISH BOR, KETISH YO'Q ─────────────────────────────────
        diff_since_check_in = (now - log.check_in_time).total_seconds()
        # 3 soniyalik avto-skaner ketma-ket ikki kadrda kelishni ketishga aylantirib yubormasligi uchun 5s himoya
        if diff_since_check_in < 5:
            return {
                'success': True,
                'action_type': 'already_checked_in',
                'is_duplicate': True,
                'staff_name': f"{getattr(staff, 'first_name', '')} {getattr(staff, 'last_name', '')}".strip(),
                'role': getattr(staff, 'role_title', 'Xodim'),
                'check_in_time': log.check_in_time.strftime('%H:%M:%S'),
                'is_late': log.is_late,
                'late_minutes': log.late_minutes,
                'fine_percent': float(log.fine_percent_applied),
                'fine_amount': float(log.fine_amount_deducted),
                'match_score': match_score,
                'message': f"Kelishingiz soat {log.check_in_time.strftime('%H:%M')} da qayd etildi!"
            }
        action_type = "ketish"
        log.check_out_time = now

    elif log.check_in_time and log.check_out_time:
        # ─── 3. ISTALGAN VAQTDA QAYTA KIRISH / KETISH VAQTINI YANGILASH ──
        action_type = "ketish"
        log.check_out_time = now
    else:
        action_type = "kelish"
        log.check_in_time = now

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
            print(f"Scan photo save error: {e}")

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
                    details=f"Kelgan vaqti: {now.strftime('%H:%M:%S')} (Belgilangan: {expected_time.strftime('%H:%M')}). Kademeli jarima foizi: {log.fine_percent_applied}%. Shtraf: {log.fine_amount_deducted:,.0f} so'm. O'xshashlik: {match_score}%."
                )
            else:
                AuditLog.objects.create(
                    action_type='face_id',
                    actor=user,
                    actor_name=staff_full_name,
                    title=f"Ishga o'z vaqtida keldi ({now.strftime('%H:%M:%S')})",
                    details=f"Face ID orqali davomatdan o'tdi. O'xshashlik: {match_score}%."
                )
        else:
            AuditLog.objects.create(
                action_type='face_id',
                actor=user,
                actor_name=staff_full_name,
                title=f"Ishdan ketish qayd etildi ({now.strftime('%H:%M:%S')})",
                details=f"Face ID orqali ketish qayd etildi. O'xshashlik: {match_score}%."
            )
    except Exception as e:
        print(f"Audit log write error: {e}")

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
            f"Muvaffaqiyatli qayd etildi! ({'Ketish' if action_type == 'ketish' else 'Kelish'})" +
            (f" Diqqat: {log.late_minutes} daqiqa kechikdingiz (-{log.fine_percent_applied}% | {log.fine_amount_deducted:,.0f} so'm)." if log.is_late and action_type == 'kelish' else "")
        )
    }


# ─────────────────────────────────────────────────────────────
# 10. OYLIK MAOSH, KECHIKISH JARIMALARI VA AVANSLARNI HISOBLASH
# ─────────────────────────────────────────────────────────────

def calculate_staff_salary_summary(staff, year: int, month: int):
    """
    Muayyan oy uchun xodimning to'liq oylik hisob-kitobini chiqaradi:
    Formula: Yakuniy Oylik = Asosiy Maosh - (Kechikish Shtraflari) - (Tasdiqlangan Avanslar) + (Bonuslar)
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
