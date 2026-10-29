"""
HUMO O'quv markazi — Admin Views Aggregator (Backward Compatibility)
2800+ qatorli monolit fayl quyidagi 3 ta mustaqil modulga ajratildi:
  - student_views.py : Dashboard, guruhlar, davomat jurnali, o'quvchilar, arxiv va chat
  - payment_views.py : To'lovlar, qarzdorlar ORM filtrlari, statistika va Excel eksport
  - face_id_views.py : Xodimlar, Face ID biometrik davomat, oylik maosh, bonus, avans va audit
"""
from .student_views import *  # noqa: F401,F403
from .payment_views import *  # noqa: F401,F403
from .face_id_views import *  # noqa: F401,F403
from .payment_views import (  # noqa: F401 — ichki yordamchi funksiyalar eksporti
    _build_payments_data,
    _clean_amount,
    _fmt_money,
    _get_student_person_key,
    _is_ajax,
    _is_free_student,
    _is_mahalla_student,
    _is_text_payment,
    _normalize_phone,
    _payment_paid_info,
    filter_students_by_payment_status,
    get_active_students_qs,
    get_payment_type_aggregates,
    normalize_month_str,
)
from .face_id_views import _safe_decimal  # noqa: F401
