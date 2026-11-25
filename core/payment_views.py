"""
HUMO O'quv markazi — Payment Views & Services (payment_views.py)
To'lovlar, qarzdorlar filtri, ORM yordamchi funksiyalar va Excel eksport.
"""
import calendar
import logging
import re
from datetime import date, datetime

logger = logging.getLogger(__name__)

try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    relativedelta = None

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from urllib.parse import quote_plus
from django.conf import settings
from django.contrib import messages
from django.db.models import Exists, OuterRef, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .attendance_service import compute_student_attendance_stats
from .auth_views import _admin_only, admin_required, is_admin_or_manager, is_manager_user
from .models import (
    Attendance, AuditLog, DeletedStudent, Group, MonthlyPayment, Student, Teacher, TimeSlot
)
from .payment_excel import generate_payments_excel
from .schedule_helper import count_scheduled_classes_in_month
from .sms_service import get_month_name_uz
from .teacher_views import _populate_excel_sheet, _student_visible_in_month


# ─────────────────────────────────────────────────────────────
# 1. UMUMIY YORDAMCHI FUNKSIYALAR (UTILS & ORM QUERYSET HELPERS)
# ─────────────────────────────────────────────────────────────

def _is_ajax(request) -> bool:
    """So'rov AJAX / Fetch orqali kelganligini tekshiradi."""
    return bool(
        request.headers.get('x-requested-with') == 'XMLHttpRequest'
        or request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest'
        or request.POST.get('ajax') in ('1', 'true', 'True')
        or request.POST.get('is_ajax') in ('1', 'true', 'True')
        or request.GET.get('ajax') in ('1', 'true', 'True')
        or 'application/json' in request.headers.get('Accept', '')
        or 'application/json' in request.META.get('HTTP_ACCEPT', '')
    )


def normalize_month_str(req_month=None) -> tuple[str, int, int]:
    """
    Har qanday oy satrini ('2026-9', ' 2026-10 ', None, '') qat'iy 'YYYY-MM'
    formatiga keltiradi va (month_str, year, month) qaytaradi.
    """
    today = date.today()
    try:
        y, m = map(int, str(req_month or '').strip().split('-')[:2])
        if not (1 <= m <= 12):
            raise ValueError
    except Exception:
        y, m = today.year, today.month
    return f"{y:04d}-{m:02d}", y, m


def _clean_amount(val) -> int:
    """
    To'lov qiymatidan sof raqamni ajratib oladi.
    500000, '500 000', '500,000', '500000.00', '500 000 so'm' -> int.
    Matn ('mahalla', 'B.U') yoki bo'sh bo'lsa -> 0.
    """
    if not val:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip().replace('\xa0', ' ')
    if not s or s.lower() in ('0', 'none', '-', 'null', '0.0', '0.00'):
        return 0
    for w in ("so'm", "som", "so`m", "uzs"):
        s = re.sub(re.escape(w), '', s, flags=re.IGNORECASE).strip()
    if '.' in s and s.replace('.', '').replace(' ', '').replace(',', '').isdigit():
        parts = s.replace(' ', '').replace(',', '').split('.')
        # Agar '500.000' (minglik ajratuvchi) bo'lsa -> 500000, '500000.00' (tiyin) bo'lsa -> 500000
        if len(parts) == 2 and len(parts[1]) <= 2:
            try:
                return int(float(s.replace(' ', '').replace(',', '')))
            except Exception:
                pass
    digits = "".join(c for c in s if c.isdigit())
    return int(digits) if digits else 0


def _is_text_payment(val) -> bool:
    """
    To'lov katagiga matn kiritilganligini tekshiradi ('mahalla', 'B.U', 'tekin', 'bepul').
    Raqamli formatlar ('500 000', '500,000', '500000.00', '0', '0.00') matn EMAS.
    """
    if not val:
        return False
    s = str(val).strip().replace('\xa0', ' ')
    if not s or s.lower() in ('0', '-', 'none', 'null', '0.0', '0.00'):
        return False
    for w in ("so'm", "som", "so`m", "uzs"):
        s = re.sub(re.escape(w), '', s, flags=re.IGNORECASE).strip()
    digits = s.replace(' ', '').replace("'", '').replace(',', '').replace('.', '')
    return bool(digits) and not digits.isdigit()


def _is_mahalla_student(student, pay=None) -> bool:
    """
    O'quvchi mahalla toifasiga kirishini tekshiradi:
      1. Student.student_type == 'mahalla' yoki student.student_type != 'paid'
      2. Student.is_local == True yoki Student.is_mahalla == True
      3. To'lov summasiga (amount_paid) yoki izohiga (notes) 'mahalla' yoki 'imtiyoz' yozilgan
    """
    st_type = str(getattr(student, 'student_type', 'paid') or '').strip().lower()
    if st_type in ('mahalla', 'imtiyoz', 'imtiyozli') or (st_type and st_type != 'paid'):
        return True
    if getattr(student, 'is_local', False) or getattr(student, 'is_mahalla', False):
        return True
    if pay:
        raw_amt = str(getattr(pay, 'amount_paid', '') or '').strip().lower()
        raw_notes = str(getattr(pay, 'notes', '') or '').strip().lower()
        if any(w in raw_amt or w in raw_notes for w in ('mahalla', 'imtiyoz', 'imtiyozli')):
            return True
    return False


def _is_free_student(student, pay=None) -> bool:
    """O'quvchi bepul / imtiyozli ('bepul', 'tekin', 'free', 'grant', 'imtiyoz') ekanligini tekshiradi."""
    if getattr(student, 'is_free', False) or getattr(student, 'is_privileged', False):
        return True
    if pay:
        raw_amt = str(getattr(pay, 'amount_paid', '') or '').strip().lower()
        raw_notes = str(getattr(pay, 'notes', '') or '').strip().lower()
        free_keywords = ('bepul', 'tekin', 'free', 'grant', 'imtiyoz', 'imtiyozli')
        if any(k in raw_amt or k in raw_notes for k in free_keywords):
            return True
    return False


def _payment_paid_info(pay) -> tuple[str, int, bool, bool]:
    """
    To'lov yozuvi bo'yicha YAGONA va qat'iy "to'langanmi?" tekshiruvi.
    Qaytaradi: (raw_paid, num_paid, has_text_payment, is_paid)
      is_paid = True, agar:
        - amount_paid > 0 (raqamli summa), YOKI
        - amount_paid ga matn yozilgan ('B.U', 'tekin' va h.k.), YOKI
        - status == 'paid'
    """
    if not pay:
        return '', 0, False, False
    raw_paid = str(pay.amount_paid or '').strip()
    num_paid = _clean_amount(raw_paid)
    has_text = _is_text_payment(raw_paid)
    is_paid = (num_paid > 0) or has_text or (pay.status == 'paid')
    return raw_paid, num_paid, has_text, is_paid


def _normalize_phone(phone_str) -> str:
    if not phone_str:
        return ""
    digits = re.sub(r'\D', '', str(phone_str))
    return digits[-9:] if len(digits) >= 9 else digits


def _get_student_person_key(student) -> str:
    """Jismoniy bitta o'quvchini telefon va ism orqali unikal aniqlash kaliti."""
    clean_p = _normalize_phone(getattr(student, 'phone_1', ''))
    fn = re.sub(r'[^a-zA-Z0-9]', '', str(getattr(student, 'first_name', '') or '').lower())
    if clean_p and fn:
        return f"p_{clean_p}_{fn}"
    if clean_p:
        return f"p_{clean_p}"
    ln = re.sub(r'[^a-zA-Z0-9]', '', str(getattr(student, 'last_name', '') or '').lower())
    return f"name_{fn}_{ln}" if (fn or ln) else f"id_{student.id}"


def _enforce_archive_exclusions():
    """
    Arxiv jadvalida (`DeletedStudent`) mavjud bo'lgan har qanday o'quvchining
    asosiy `Student` yozuvi qat'iy ravishda `is_active=False` bo'lishini ta'minlaydi.
    (Agar eski arxiv yozuvlarida `student_id` bo'sh bo'lsa, ism-familiya va telefon orqali bog'laydi).
    """
    # 1. student_id ko'rsatilgan arxiv yozuvlari bo'yicha is_active=False qilish
    deleted_ids = list(
        DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)
    )
    if deleted_ids:
        Student.objects.filter(id__in=deleted_ids, is_active=True).update(is_active=False)

    # 2. student_id bo'sh bo'lgan eski DeletedStudent yozuvlarini tekshirish
    for ds in DeletedStudent.objects.filter(student_id__isnull=True):
        fn = (ds.first_name or '').strip()
        ln = (ds.last_name or '').strip()
        if not (fn and ln) and ds.student_name:
            parts = ds.student_name.strip().split(None, 1)
            fn = parts[0] if parts else ''
            ln = parts[1] if len(parts) > 1 else ''
        if fn:
            qs = Student.objects.filter(first_name__iexact=fn, last_name__iexact=ln)
            if ds.phone_1:
                qs = qs.filter(phone_1=ds.phone_1.strip())
            matched = qs.first()
            if matched:
                ds.student_id = matched.id
                ds.save(update_fields=['student_id'])
                if matched.is_active:
                    matched.is_active = False
                    matched.save(update_fields=['is_active'])

    # 3. left_date belgilangan (ketgan) o'quvchilar ham faol hisoblanmasin
    Student.objects.filter(left_date__isnull=False, is_active=True).update(is_active=False)


def heal_student_group_links():
    """Backward compatibility alias: faqat arxiv cheklovlarini sinxronlaydi."""
    _enforce_archive_exclusions()


def get_active_students_qs(group=None, group_id=None, search_q: str = '', require_group: bool = True):
    """
    Faol (`is_active=True`), ketmagan (`left_date__isnull=True`), guruhga biriktirilgan
    (`time_slot__isnull=False`) va arxivda (`DeletedStudent`) bo'lmagan o'quvchilar QuerySet'i.
    Arxivdagi yoki o'chirilgan o'quvchilar to'lovlar, qarzdorlar va hisobotlarda MUTLAQO ko'rinmaydi.
    """
    _enforce_archive_exclusions()
    deleted_subquery = DeletedStudent.objects.filter(student_id=OuterRef('pk'))
    qs = (
        Student.objects.annotate(_is_deleted=Exists(deleted_subquery))
        .filter(
            _is_deleted=False,
            is_deleted=False,
            is_active=True,
            left_date__isnull=True,
            time_slot__isnull=False,
            time_slot__group__isnull=False,
        )
        .select_related('time_slot__group__teacher', 'time_slot__group')
    )
    if group is not None:
        qs = qs.filter(time_slot__group=group)
    elif group_id and str(group_id).isdigit():
        qs = qs.filter(time_slot__group_id=int(group_id))

    if search_q:
        q_filter = Q()
        for w in search_q.split():
            q_filter |= (
                Q(first_name__icontains=w)
                | Q(last_name__icontains=w)
                | Q(phone_1__icontains=w)
                | Q(phone_2__icontains=w)
            )
        qs = qs.filter(q_filter)

    return qs


def filter_students_by_payment_status(students_qs, req_month: str, status_filter: str = 'all'):
    """
    Django ORM darajasida (`Exists` va `OuterRef` orqali) o'quvchilarni
    'paid' (to'laganlar), 'unpaid' (qarzdorlar) va 'local' (mahalla) bo'yicha filtrlash.
    Shartlar:
      1. Faqat tanlangan oyda yoki undan oldin markazga qo'shilgan o'quvchilar hisobga olinadi (joined_date <= month_end).
      2. Tanlangan oyda (`req_month`) `amount_paid` bo'sh/'0' bo'lmasa YOKI `status='paid'`
         bo'lsa -> o'quvchi to'lagan (`_has_paid=True`).
      3. To'lagan o'quvchi QAT'IY ravishda `unpaid` ro'yxatidan chiqariladi (`_has_paid=False`).
      4. Faqat to'lov qilmagan, mahalla (`is_local=False`) va bepul bo'lmaganlar `unpaid` ga kiradi.
    """
    _enforce_archive_exclusions()
    req_month, req_year, req_mon = normalize_month_str(req_month)
    max_day_in_month = calendar.monthrange(req_year, req_mon)[1]
    month_end_date = date(req_year, req_mon, max_day_in_month)
    empty_values = ['', '0', '0.0', '0.00', '-', 'none', 'None', 'null', 'NULL']
    deleted_subquery = DeletedStudent.objects.filter(student_id=OuterRef('pk'))

    students_qs = students_qs.annotate(_is_deleted=Exists(deleted_subquery)).filter(
        _is_deleted=False,
        is_active=True,
        left_date__isnull=True,
        time_slot__isnull=False,
        time_slot__group__isnull=False,
        joined_date__lte=month_end_date,
    )

    paid_payment_qs = MonthlyPayment.objects.filter(
        student_id=OuterRef('pk'),
        month=req_month,
    ).filter(
        ~Q(amount_paid__in=empty_values) | Q(status='paid')
    )

    mahalla_payment_qs = MonthlyPayment.objects.filter(
        student_id=OuterRef('pk'),
        month=req_month,
    ).filter(
        Q(amount_paid__icontains='mahalla')
        | Q(notes__icontains='mahalla')
        | Q(amount_paid__icontains='imtiyoz')
        | Q(notes__icontains='imtiyoz')
    )

    free_payment_qs = MonthlyPayment.objects.filter(
        student_id=OuterRef('pk'),
        month=req_month,
    ).filter(
        Q(amount_paid__icontains='bepul')
        | Q(amount_paid__icontains='tekin')
        | Q(amount_paid__icontains='free')
        | Q(amount_paid__icontains='grant')
        | Q(notes__icontains='bepul')
        | Q(notes__icontains='tekin')
        | Q(notes__icontains='free')
        | Q(notes__icontains='grant')
    )

    qs = students_qs.annotate(
        _has_paid=Exists(paid_payment_qs),
        _has_mahalla_pay=Exists(mahalla_payment_qs),
        _has_free_pay=Exists(free_payment_qs),
    )

    if status_filter == 'paid':
        return qs.filter(_has_paid=True)
    if status_filter == 'unpaid':
        return qs.filter(
            _has_paid=False,
            student_type='paid',
            is_local=False,
            _has_mahalla_pay=False,
            _has_free_pay=False,
        ).exclude(
            Q(student_type__iexact='mahalla') | Q(is_local=True)
        )
    if status_filter == 'local':
        return qs.filter(
            Q(is_local=True)
            | Q(student_type__iexact='mahalla')
            | ~Q(student_type='paid')
            | Q(_has_mahalla_pay=True)
        )
    return qs


def _fmt_money(val: int) -> str:
    return f"{val:,}".replace(',', ' ')


# ─────────────────────────────────────────────────────────────
# 2. TO'LOVLAR JADVALI VA STATISTIKA GENERATORI
# ─────────────────────────────────────────────────────────────

def _build_payments_data(students_qs, req_month, status_filter='all', is_group_specific=False):
    """
    O'quvchilar va ularning to'lovlari bo'yicha jadval satrlari hamda moliyaviy statistikani qaytaradi.
    Qat'iy qoidalar:
      1. Faqat tanlangan oyda yoki undan oldin markazga qo'shilgan o'quvchilar (`joined_date <= month_end`) olinadi.
         Keyingi oylarda kelgan o'quvchilar o'tgan oylarga mutlaqo hisoblanmaydi va jadvalda ko'rinmaydi.
      2. `amount_paid > 0`, matnli to'lov yoki `status == 'paid'` bo'lsa -> FAQAT "To'lagan" ('paid').
      3. To'lagan o'quvchi qarzdorlar ('unpaid') va kechikkanlar ('overdue') ro'yxatiga MUTLAQO kirmaydi.
      4. Faqat haqiqatda to'lov qilmagan, mahalla va bepul bo'lmagan o'quvchilar qarzdor ('unpaid') hisoblanadi.
    """
    today = date.today()
    req_month, req_year, req_mon = normalize_month_str(req_month)
    status_filter = (status_filter or 'all').strip().lower()
    max_day_in_month = calendar.monthrange(req_year, req_mon)[1]
    month_end_date = date(req_year, req_mon, max_day_in_month)
    curr_month_str = today.strftime('%Y-%m')

    _enforce_archive_exclusions()
    deleted_subquery = DeletedStudent.objects.filter(student_id=OuterRef('pk'))
    base_students_filter = students_qs.annotate(_is_deleted=Exists(deleted_subquery)).filter(
        _is_deleted=False,
        is_active=True,
        left_date__isnull=True,
        time_slot__isnull=False,
        time_slot__group__isnull=False,
        joined_date__lte=month_end_date,
    )
    if status_filter == 'unpaid':
        base_students_filter = base_students_filter.filter(
            student_type='paid',
            is_local=False,
        ).exclude(
            Q(student_type__iexact='mahalla') | Q(is_local=True)
        )

    students_list = list(
        base_students_filter
        .select_related('time_slot__group__teacher', 'time_slot__group')
        .distinct()
        .order_by('first_name', 'last_name', 'id')
    )
    student_ids = [s.id for s in students_list]

    payments = list(
        MonthlyPayment.objects.filter(
            student_id__in=student_ids,
            student__is_active=True,
            student__left_date__isnull=True,
            month=req_month,
        )
    )
    payment_map = {}
    for p in payments:
        prev = payment_map.get(p.student_id)
        if prev is None or (not _payment_paid_info(prev)[3] and _payment_paid_info(p)[3]):
            payment_map[p.student_id] = p

    all_rows = []
    total_rate_sum = paid_count = unpaid_count = overdue_count = local_count = free_count = debt_sum = 0

    for s in students_list:
        pay = payment_map.get(s.id)
        raw_paid, num_paid, has_text_payment, is_paid = _payment_paid_info(pay)
        is_mahalla = _is_mahalla_student(s, pay)
        is_free = not is_mahalla and _is_free_student(s, pay)

        grp = s.time_slot.group if s.time_slot else None
        rate_val = s.payment_rate or 0
        total_rate_sum += rate_val

        pay_date_obj = (
            pay.payment_date if (pay and pay.payment_date)
            else (pay.created_at.date() if (pay and pay.created_at) else None)
        )

        has_paid_val = is_paid or is_mahalla or is_free
        if pay_date_obj and has_paid_val:
            yr, mn = pay_date_obj.year, pay_date_obj.month + 1
            if mn > 12:
                mn, yr = 1, yr + 1
            due_date = date(yr, mn, min(pay_date_obj.day, calendar.monthrange(yr, mn)[1]))
        else:
            joined_d = s.joined_date or today
            due_date = date(req_year, req_mon, min(joined_d.day, max_day_in_month))

        is_overdue = False
        delay_days = 0

        if is_mahalla:
            st_code = 'local'
            debt_val = 0
            local_count += 1
            if num_paid > 0:
                paid_count += 1
                status_display = f"To'langan ({_fmt_money(num_paid)} so'm)"
            elif has_text_payment and raw_paid.lower() != 'mahalla':
                status_display = f"Mahalla ({raw_paid})"
            else:
                status_display = "Mahalla"
        elif is_free and num_paid == 0:
            st_code = 'free'
            debt_val = 0
            free_count += 1
            status_display = f"Bepul ({raw_paid})" if raw_paid else "Bepul"
        elif is_paid:
            st_code = 'paid'
            debt_val = 0
            paid_count += 1
            if num_paid > 0:
                status_display = f"To'langan ({_fmt_money(num_paid)} so'm)"
            elif has_text_payment:
                status_display = f"To'langan ({raw_paid})"
            else:
                status_display = "To'langan"
        else:
            st_code = 'unpaid'
            debt_val = rate_val
            debt_sum += debt_val
            unpaid_count += 1

            if req_month < curr_month_str:
                is_overdue = True
                delay_days = max(1, (today - due_date).days)
            elif today > due_date:
                is_overdue = True
                delay_days = (today - due_date).days

            if is_overdue:
                overdue_count += 1
                status_display = "Muddati o'tgan"
            else:
                status_display = "To'lanmagan"

        all_rows.append({
            'index': 0,
            'student': s,
            'student_id': s.id,
            'student_name': f"{s.first_name} {s.last_name}",
            'is_local': is_mahalla,
            'is_mahalla': is_mahalla,
            'is_free': is_free,
            'is_paid': is_paid,
            'group': grp,
            'group_name': grp.name if grp else "-",
            'group_id': grp.id if grp else None,
            'subject_name': grp.subject_name if grp else "-",
            'teacher_name': str(grp.teacher) if (grp and grp.teacher) else "-",
            'joined_date': s.joined_date.strftime('%d.%m.%Y') if s.joined_date else "-",
            'joined_date_iso': s.joined_date.strftime('%Y-%m-%d') if s.joined_date else "",
            'due_date': due_date,
            'due_date_str': due_date.strftime('%d.%m.%Y'),
            'is_overdue': is_overdue,
            'delay_days': delay_days,
            'delay_text': f"{delay_days} kun kechikdi" if is_overdue else "",
            'payment_date': pay_date_obj.strftime('%d.%m.%Y') if pay_date_obj else "",
            'payment_date_iso': pay_date_obj.strftime('%Y-%m-%d') if pay_date_obj else "",
            'payment_rate': rate_val,
            'payment_rate_formatted': _fmt_money(rate_val),
            'amount_paid': raw_paid,
            'amount_paid_formatted': _fmt_money(num_paid) if num_paid > 0 else (raw_paid or "-"),
            'num_paid': num_paid,
            'has_text_payment': has_text_payment,
            'status_code': st_code,
            'status_display': status_display,
            'phone': f"{s.phone_1} {s.phone_2 or ''}".strip(),
            'notes': pay.notes if pay else "",
            'payment_id': pay.id if pay else None,
            'payment_type': pay.payment_type if (pay and getattr(pay, 'payment_type', None)) else 'cash',
            'payment_type_display': (
                pay.get_payment_type_display()
                if (pay and hasattr(pay, 'get_payment_type_display'))
                else "Naqd pul (Cash)"
            ),
            'debt': debt_val,
            'debt_formatted': _fmt_money(debt_val),
        })

    # Filtr bo'yicha saralash:
    # - 'paid': faqat haqiqatda to'lov qilganlar (yoki pul to'lagan mahalla o'quvchilari)
    # - 'unpaid': faqat to'lov qilmagan, mahalla bo'lmagan va bepul bo'lmagan haqiqiy qarzdorlar
    rows = []
    for r in all_rows:
        is_truly_unpaid = (
            r['status_code'] == 'unpaid'
            and not r['is_paid']
            and not r['is_mahalla']
            and not r['is_free']
            and getattr(r['student'], 'student_type', 'paid') == 'paid'
            and not getattr(r['student'], 'is_local', False)
        )
        if status_filter == 'unpaid' and not is_truly_unpaid:
            continue
        if status_filter == 'overdue' and not (is_truly_unpaid and r['is_overdue']):
            continue
        if status_filter == 'paid' and not (r['status_code'] == 'paid' or (r['is_mahalla'] and r['num_paid'] > 0)):
            continue
        if status_filter == 'local' and not (r['is_mahalla'] or r['has_text_payment']):
            continue
        if status_filter == 'free' and r['status_code'] != 'free':
            continue
        rows.append(r)

    for i, r in enumerate(rows, start=1):
        r['index'] = i

    # To'lov turlari va tushumlar hisobi
    totals = {'cash': 0, 'terminal': 0, 'payme': 0, 'click': 0}
    counts = {'cash': 0, 'terminal': 0, 'payme': 0, 'click': 0}
    daily_collected = total_paid_sum = 0

    for pay in payments:
        num_paid = _clean_amount(pay.amount_paid)
        if num_paid > 0:
            total_paid_sum += num_paid
            pay_date = pay.payment_date or (pay.created_at.date() if pay.created_at else None)
            if pay_date == today:
                daily_collected += num_paid
            ptype = getattr(pay, 'payment_type', 'cash') or 'cash'
            if ptype not in totals:
                ptype = 'cash'
            totals[ptype] += num_paid
            counts[ptype] += 1

    def _pct(val):
        return round((val / total_paid_sum) * 100) if total_paid_sum > 0 else 0

    stats = {
        'total_students': len(all_rows),
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
        'overdue_count': overdue_count,
        'local_count': local_count,
        'free_count': free_count,
        'total_rate': total_rate_sum,
        'total_rate_formatted': _fmt_money(total_rate_sum),
        'total_paid': total_paid_sum,
        'total_paid_formatted': _fmt_money(total_paid_sum),
        'total_cash': totals['cash'],
        'total_cash_formatted': _fmt_money(totals['cash']),
        'total_terminal': totals['terminal'],
        'total_terminal_formatted': _fmt_money(totals['terminal']),
        'total_payme': totals['payme'],
        'total_payme_formatted': _fmt_money(totals['payme']),
        'total_click': totals['click'],
        'total_click_formatted': _fmt_money(totals['click']),
        'count_cash': counts['cash'],
        'count_terminal': counts['terminal'],
        'count_payme': counts['payme'],
        'count_click': counts['click'],
        'pct_cash': _pct(totals['cash']),
        'pct_terminal': _pct(totals['terminal']),
        'pct_payme': _pct(totals['payme']),
        'pct_click': _pct(totals['click']),
        'debt_sum': debt_sum,
        'debt_sum_formatted': _fmt_money(debt_sum),
        'daily_collected': daily_collected,
        'daily_collected_formatted': _fmt_money(daily_collected),
        'req_year': req_year,
    }
    return rows, stats


def get_payment_type_aggregates(month=None, start_date=None, end_date=None):
    """To'lov turlari (Naqd, Terminal, Payme, Click) bo'yicha faqat faol o'quvchilar tushumlarini qaytaradi."""
    _enforce_archive_exclusions()
    deleted_ids = DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)
    qs = MonthlyPayment.objects.filter(
        student__is_active=True,
        student__left_date__isnull=True,
        student__time_slot__isnull=False,
        student__time_slot__group__isnull=False,
    ).exclude(student_id__in=deleted_ids)
    if month:
        norm_m, _, _ = normalize_month_str(month)
        qs = qs.filter(month=norm_m)
    if start_date:
        qs = qs.filter(payment_date__gte=start_date)
    if end_date:
        qs = qs.filter(payment_date__lte=end_date)

    totals = {'cash': 0, 'terminal': 0, 'payme': 0, 'click': 0}
    counts = {'cash': 0, 'terminal': 0, 'payme': 0, 'click': 0}
    total_all = 0

    for p in qs:
        num = _clean_amount(p.amount_paid)
        if num > 0:
            ptype = p.payment_type if getattr(p, 'payment_type', None) in totals else 'cash'
            totals[ptype] += num
            counts[ptype] += 1
            total_all += num

    def _pct(val):
        return round((val / total_all) * 100) if total_all > 0 else 0

    return {
        'total_cash': totals['cash'],
        'total_cash_formatted': _fmt_money(totals['cash']),
        'total_terminal': totals['terminal'],
        'total_terminal_formatted': _fmt_money(totals['terminal']),
        'total_payme': totals['payme'],
        'total_payme_formatted': _fmt_money(totals['payme']),
        'total_click': totals['click'],
        'total_click_formatted': _fmt_money(totals['click']),
        'count_cash': counts['cash'],
        'count_terminal': counts['terminal'],
        'count_payme': counts['payme'],
        'count_click': counts['click'],
        'pct_cash': _pct(totals['cash']),
        'pct_terminal': _pct(totals['terminal']),
        'pct_payme': _pct(totals['payme']),
        'pct_click': _pct(totals['click']),
        'total_all': total_all,
        'total_all_formatted': _fmt_money(total_all),
    }


# ─────────────────────────────────────────────────────────────
# 3. TO'LOV SAHIFALARI VA EXCEL EKSPORT VIEW'LARI
# ─────────────────────────────────────────────────────────────

@admin_required
def admin_payments_all(request):
    """Barcha guruhlar bo'yicha umumiy to'lovlar sahifasi."""
    if not is_admin_or_manager(request):
        return redirect('home')

    req_month, _, _ = normalize_month_str(request.GET.get('month'))
    group_id = request.GET.get('group_id', '').strip()
    status_filter = (request.GET.get('status') or 'all').strip().lower()
    search_q = request.GET.get('q', '').strip()

    groups = Group.objects.select_related('teacher').order_by('name')
    students_qs = get_active_students_qs(group_id=group_id, search_q=search_q)
    rows, stats = _build_payments_data(
        students_qs, req_month, status_filter, is_group_specific=group_id.isdigit()
    )

    return render(request, 'admin_payments_all.html', {
        'rows': rows,
        'groups': groups,
        'current_month': req_month,
        'selected_group_id': int(group_id) if group_id.isdigit() else '',
        'selected_status': status_filter,
        'search_query': search_q,
        'stats': stats,
    })


@admin_required
def admin_group_payments(request, group_id):
    """Alohida bir guruh uchun to'lovlar sahifasi."""
    if not is_admin_or_manager(request):
        return redirect('home')

    group = get_object_or_404(Group.objects.select_related('teacher'), id=group_id)
    req_month, _, _ = normalize_month_str(request.GET.get('month'))
    status_filter = (request.GET.get('status') or 'all').strip().lower()
    search_q = request.GET.get('q', '').strip()

    students_qs = get_active_students_qs(group=group, search_q=search_q)
    rows, stats = _build_payments_data(students_qs, req_month, status_filter, is_group_specific=True)

    return render(request, 'admin_group_payments.html', {
        'group': group,
        'rows': rows,
        'current_month': req_month,
        'selected_status': status_filter,
        'search_query': search_q,
        'stats': stats,
    })


@admin_required
def admin_payment_save(request):
    """Admin va Menejer tomonidan to'lovni saqlash / yangilash."""
    is_ajax = _is_ajax(request)
    if not is_admin_or_manager(request):
        if is_ajax:
            return JsonResponse({'status': 'error', 'msg': 'Ruxsat berilmagan'}, status=403)
        messages.error(request, "Ruxsat berilmagan!")
        return redirect('home')

    if request.method != 'POST':
        if is_ajax:
            return JsonResponse({'status': 'error', 'msg': "Noto'g'ri so'rov"}, status=400)
        return redirect('admin_payments_all')

    student_id = request.POST.get('student_id')
    if not student_id:
        if is_ajax:
            return JsonResponse({'status': 'error', 'msg': "O'quvchi ko'rsatilmadi"}, status=400)
        messages.error(request, "O'quvchi tanlanmadi!")
        return redirect('admin_payments_all')

    month, _, _ = normalize_month_str(request.POST.get('month'))
    amount_raw_str = str(request.POST.get('amount') or request.POST.get('amount_paid') or '0').strip()
    payment_type = request.POST.get('payment_type', 'cash').strip()
    valid_types = {t[0] for t in MonthlyPayment.PAYMENT_TYPE_CHOICES}
    if payment_type not in valid_types:
        payment_type = 'cash'
    notes = request.POST.get('notes', '').strip()

    student = get_object_or_404(Student, id=student_id)
    amount_num = _clean_amount(amount_raw_str)
    has_text = _is_text_payment(amount_raw_str)
    is_mahalla_user = (
        getattr(student, 'is_local', False)
        or getattr(student, 'is_mahalla', False)
        or ('mahalla' in amount_raw_str.lower())
        or ('mahalla' in notes.lower())
    )

    if amount_num > 0:
        amount_to_save = str(amount_num)
        new_status = 'paid'
    elif is_mahalla_user:
        amount_to_save = amount_raw_str if amount_raw_str else 'mahalla'
        new_status = 'paid'
    else:
        amount_to_save = amount_raw_str
        new_status = 'paid' if has_text else 'unpaid'

    joined_date_raw = request.POST.get('joined_date', '').strip()
    if joined_date_raw:
        try:
            new_joined = datetime.strptime(joined_date_raw, '%Y-%m-%d').date()
            if new_joined > date.today():
                if is_ajax:
                    return JsonResponse({'status': 'error', 'msg': "Kelgan sana kelajakdagi sana bo'lishi mumkin emas!"}, status=400)
                messages.error(request, "Kelgan sana kelajakdagi sana bo'lishi mumkin emas!")
            elif student.joined_date != new_joined:
                student.joined_date = new_joined
                student.save(update_fields=['joined_date'])
        except Exception:
            pass

    # O'quvchi qo'shilgan oydan oldingi oylar uchun to'lov yaratilishiga yo'l qo'ymaslik
    st_joined = student.joined_date or date.today()
    st_joined_month = st_joined.strftime('%Y-%m')
    if month < st_joined_month:
        err_msg = (
            f"O'quvchi {st_joined.strftime('%d.%m.%Y')} sanasida guruhga qo'shilgan. "
            f"O'quvchi qo'shilishidan oldingi oy ({month}) uchun to'lov kiritish mumkin emas!"
        )
        if is_ajax:
            return JsonResponse({'status': 'error', 'msg': err_msg}, status=400)
        messages.error(request, err_msg)
        return redirect('admin_payments_all')

    mp, _ = MonthlyPayment.objects.update_or_create(
        student=student,
        month=month,
        defaults={
            'amount_paid': amount_to_save,
            'payment_type': payment_type,
            'payment_date': date.today(),
            'status': new_status,
            'notes': notes,
        },
    )

    try:
        grp_name = student.time_slot.group.name if (student.time_slot and student.time_slot.group) else '-'
        AuditLog.objects.create(
            action_type='payment',
            actor=request.user if getattr(request, 'user', None) and request.user.is_authenticated else None,
            actor_name="Menejer" if is_manager_user(request) else "Direktor / Admin",
            title=f"To'lov qayd etildi: {student.first_name} {student.last_name} ({amount_to_save} - {mp.get_payment_type_display()})",
            details=f"Guruh: {grp_name}, Oy: {month}, Turi: {mp.get_payment_type_display()}, Holat: {new_status}",
        )
    except Exception:
        pass

    if is_ajax:
        rate_val = student.payment_rate or 0
        due_date_str = ''
        if mp.payment_date and (amount_num > 0 or has_text or is_mahalla_user):
            if relativedelta:
                due_date_obj = mp.payment_date + relativedelta(months=1)
            else:
                yr, mn = mp.payment_date.year, mp.payment_date.month + 1
                if mn > 12:
                    mn, yr = 1, yr + 1
                due_date_obj = date(yr, mn, min(mp.payment_date.day, calendar.monthrange(yr, mn)[1]))
            due_date_str = due_date_obj.strftime('%d.%m.%Y')

        is_paid = (new_status == 'paid') or has_text or is_mahalla_user
        if is_mahalla_user:
            status_display = "Mahalla" if amount_num == 0 else f"To'langan ({amount_to_save})"
            st_code_resp, debt = 'paid', 0
        elif has_text:
            status_display = f"To'langan ({amount_to_save})"
            st_code_resp, debt = 'paid', 0
        elif is_paid:
            status_display = "To'langan"
            st_code_resp, debt = 'paid', 0
        else:
            status_display = "Qarzdor"
            st_code_resp, debt = 'unpaid', rate_val

        return JsonResponse({
            'status': 'success',
            'payment_id': mp.id,
            'amount_paid': mp.amount_paid,
            'amount_paid_num': amount_num,
            'amount_paid_formatted': _fmt_money(amount_num) if amount_num > 0 else (amount_to_save if (has_text or is_mahalla_user) else '0'),
            'payment_type': mp.payment_type,
            'payment_type_display': mp.get_payment_type_display(),
            'payment_date': mp.payment_date.strftime('%d.%m.%Y') if mp.payment_date else '',
            'payment_date_iso': mp.payment_date.strftime('%Y-%m-%d') if mp.payment_date else '',
            'due_date': due_date_str,
            'status_code': st_code_resp,
            'status_display': status_display,
            'notes': mp.notes,
            'rate': rate_val,
            'debt': debt,
            'debt_formatted': _fmt_money(debt) if debt > 0 else '0',
        })

    if amount_num > 0:
        messages.success(request, f"✅ {student}: {_fmt_money(amount_num)} so'm [{mp.get_payment_type_display()}] ({month}) saqlandi!")
    elif is_mahalla_user:
        messages.success(request, f"✅ {student}: Mahalla toifasi ({month}) saqlandi!")
    elif has_text:
        messages.success(request, f"✅ {student}: '{amount_to_save}' ({month}) saqlandi!")
    else:
        messages.warning(request, f"⚠️ {student}: {month} uchun to'lov bekor qilindi (0 so'm).")
    return redirect(request.POST.get('next') or request.META.get('HTTP_REFERER') or 'admin_payments_all')


@admin_required
def admin_payment_delete(request, id):
    """Tanlangan to'lov yozuvini bekor qilish / o'chirish."""
    if not is_admin_or_manager(request):
        return redirect('home')
    mp = get_object_or_404(MonthlyPayment, id=id)
    if request.method in ('POST', 'GET'):
        student_name = str(mp.student)
        month = mp.month
        mp.delete()
        messages.success(request, f"{student_name}ning {month} oyi uchun to'lovi o'chirildi.")
    return redirect(request.GET.get('next') or request.POST.get('next') or request.META.get('HTTP_REFERER') or 'admin_payments_all')


@admin_required
def payment_mark(request):
    """Eski payment_mark endpointi bilan moslik (backward compatibility)."""
    return admin_payment_save(request)


@admin_required
def export_payments_excel(request):
    """To'lovlar jadvalini professional Excel fayl (.xlsx) shaklida eksport qilish."""
    if not is_admin_or_manager(request):
        return redirect('home')

    req_month, _, _ = normalize_month_str(request.GET.get('month'))
    group_id = request.GET.get('group_id', '').strip()
    status_filter = (request.GET.get('status') or 'all').strip().lower()
    if request.GET.get('is_local') in ('1', 'true', 'True') or status_filter == 'local':
        status_filter = 'local'

    is_grp = group_id.isdigit()
    if is_grp:
        group = get_object_or_404(Group.objects.select_related('teacher'), id=int(group_id))
        students_qs = get_active_students_qs(group=group)
        base_title = f"Guruh To'lovlari — {group.name}"
        safe_name = "".join(c for c in group.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        base_filename = f"Toluvlar_{safe_name}"
        group_info = f"Fan: {group.subject_name} | O'qituvchi: {group.teacher or '—'}"
    else:
        students_qs = get_active_students_qs()
        base_title = "Barcha Guruhlar Umumiy To'lovlar Jadvali"
        base_filename = "Humo_Umumiy_Toluvlar"
        group_info = "Barcha guruhlar"

    status_meta = {
        'paid': ("To'liq To'laganlar", f"{base_filename}_Tolaganlar_{req_month}.xlsx"),
        'overdue': ("Kechikkanlar (Muddati O'tganlar)", f"{base_filename}_Kechikkanlar_{req_month}.xlsx"),
        'unpaid': ("Qarzdorlar (Umuman to'lamaganlar)", f"{base_filename}_Qarzdorlar_{req_month}.xlsx"),
        'local': ("Mahalladan Kelganlar", f"{base_filename}_Mahalladan_{req_month}.xlsx"),
    }
    type_label, filename = status_meta.get(status_filter, ("Barcha O'quvchilar", f"{base_filename}_{req_month}.xlsx"))

    title = f"{base_title} ({type_label})"
    subtitle = f"{group_info} | Holat: {type_label} | Davr: {req_month} | Yuklangan: {date.today().strftime('%d.%m.%Y')}"

    filter_for_data = 'all' if status_filter == 'yearly' else status_filter
    rows, stats = _build_payments_data(students_qs, req_month, status_filter=filter_for_data, is_group_specific=is_grp)
    excel_file = generate_payments_excel(rows, title=title, subtitle=subtitle, stats=stats)

    response = HttpResponse(
        excel_file.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@admin_required
def admin_export_excel(request):
    """Admin: Barcha o'qituvchilar va ularga biriktirilgan guruhlar/o'quvchilar ro'yxatini yuklab berish."""
    if not is_admin_or_manager(request):
        return redirect('home')

    curr_month, req_year, req_mon = normalize_month_str(request.GET.get('month'))
    max_d = calendar.monthrange(req_year, req_mon)[1]
    month_end_date = date(req_year, req_mon, max_d)
    payment_map = dict(MonthlyPayment.objects.filter(month=curr_month).values_list('student_id', 'amount_paid'))

    all_students = list(
        get_active_students_qs()
        .filter(joined_date__lte=month_end_date)
        .order_by(
            'time_slot__group__teacher__first_name',
            'time_slot__group__teacher__last_name',
            'time_slot__group__name',
            'first_name',
            'last_name',
        )
    )

    wb = openpyxl.Workbook()
    ws_all = wb.active
    ws_all.title = "Barcha O'quvchilar"
    _populate_excel_sheet(ws_all, all_students, payment_map)

    for t in Teacher.objects.order_by('first_name', 'last_name'):
        t_students = [s for s in all_students if s.time_slot and s.time_slot.group and s.time_slot.group.teacher_id == t.id]
        if t_students:
            ws_t = wb.create_sheet(title=f"{t.first_name} {t.last_name}"[:30])
            _populate_excel_sheet(ws_t, t_students, payment_map)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="HUMO_Oquvchilar_{curr_month}.xlsx"'
    wb.save(response)
    return response


def generate_group_attendance_excel(group, year: int, month: int) -> openpyxl.Workbook:
    """
    Guruh o'quvchilarining tanlangan oy bo'yicha yo'qlama (davomat) jurnali
    va shaxsiy ko'rsatkichlarini veb-interfeys bilan bir xil ko'rinishda
    dinamik sanalar ustunlari bilan Excel (.xlsx) sifatida shakllantiradi.
    """
    num_days = calendar.monthrange(year, month)[1]
    curr_month = f"{year:04d}-{month:02d}"
    start_date = date(year, month, 1)
    end_date = date(year, month, num_days)

    # Guruhning ushbu oyda faol va arxivda bo'lmagan o'quvchilari
    students = list(
        get_active_students_qs(group=group)
        .filter(joined_date__lte=end_date)
        .order_by('first_name', 'last_name')
    )
    students = [s for s in students if _student_visible_in_month(s, year, month)]

    # Davomat qaydlarini olish (faqat haqiqiy belgilangan present/absent)
    student_ids = [s.id for s in students]
    attendance_map = {sid: {} for sid in student_ids}
    if student_ids:
        att_records = Attendance.objects.filter(
            student_id__in=student_ids,
            date__range=(start_date, end_date),
            status__in=['present', 'absent'],
        ).values_list('student_id', 'date', 'status')
        for sid, att_date, status in att_records:
            attendance_map[sid][att_date.day] = status

    ts_scheduled_map = {
        ts.id: count_scheduled_classes_in_month(ts.days, year, month)
        for ts in group.time_slots.all()
    }

    # Workbook yaratish
    wb = openpyxl.Workbook()
    ws = wb.active
    safe_name = "".join(c for c in group.name if c.isalnum() or c in (' ', '_', '-')).strip()
    ws.title = safe_name[:28] or "Davomat"
    ws.views.sheetView[0].showGridLines = True

    # Ranglar va shriftlar
    c_title_bg = "1E293B"       # Slate 900
    c_subtitle_bg = "334155"    # Slate 700
    c_header_bg = "4F46E5"      # Indigo 600
    c_weekend_header_bg = "6366F1"  # Indigo 500 (Dam olish kunlari)
    c_border = "CBD5E1"         # Slate 300
    c_total_bg = "F1F5F9"       # Slate 100
    c_zebra_bg = "F8FAFC"       # Slate 50

    font_title = Font(name="Arial", size=13, bold=True, color="FFFFFF")
    font_sub = Font(name="Arial", size=10, italic=True, color="E2E8F0")
    font_hdr = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    font_cell = Font(name="Arial", size=10)
    font_bold = Font(name="Arial", size=10, bold=True)
    font_present = Font(name="Arial", size=11, bold=True, color="15803D")  # Yashil (+)
    font_absent = Font(name="Arial", size=11, bold=True, color="B91C1C")   # Qizil (-)

    fill_title = PatternFill(start_color=c_title_bg, end_color=c_title_bg, fill_type="solid")
    fill_sub = PatternFill(start_color=c_subtitle_bg, end_color=c_subtitle_bg, fill_type="solid")
    fill_hdr = PatternFill(start_color=c_header_bg, end_color=c_header_bg, fill_type="solid")
    fill_hdr_weekend = PatternFill(start_color=c_weekend_header_bg, end_color=c_weekend_header_bg, fill_type="solid")
    fill_present = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")  # Yashil fon
    fill_absent = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")   # Qizil fon
    fill_total = PatternFill(start_color=c_total_bg, end_color=c_total_bg, fill_type="solid")
    fill_zebra = PatternFill(start_color=c_zebra_bg, end_color=c_zebra_bg, fill_type="solid")

    thin_side = Side(border_style="thin", color=c_border)
    thick_bottom = Side(border_style="medium", color="1E293B")
    double_bottom = Side(border_style="double", color="1E293B")
    cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    header_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thick_bottom)
    total_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=double_bottom)

    align_center = Alignment(horizontal="center", vertical="center")
    align_left = Alignment(horizontal="left", vertical="center")
    align_right = Alignment(horizontal="right", vertical="center")

    total_cols = 7 + num_days
    last_col_letter = get_column_letter(total_cols)

    # Guruhning umumiy oylik ko'rsatkichlari
    group_total_present = 0
    group_total_absent = 0
    student_stats = []
    for s in students:
        s_att = attendance_map.get(s.id, {})
        ts_scheduled_classes = ts_scheduled_map.get(s.time_slot_id)
        st_stats = compute_student_attendance_stats(s_att, total_group_classes=ts_scheduled_classes)
        student_stats.append({
            'student': s,
            'present_count': st_stats['present_count'],
            'absent_count': st_stats['absent_count'],
            'total_marked': st_stats['total_count'],
            'percentage': st_stats['percentage'],
            'attendance_map': s_att,
        })
        group_total_present += st_stats['present_count']
        group_total_absent += st_stats['marked_absent_count']

    group_total_records = group_total_present + group_total_absent
    group_pct = round((group_total_present / group_total_records) * 100, 1) if group_total_records > 0 else 0.0

    # 1. Asosiy Sarlavha (Row 1)
    ws.merge_cells(f"A1:{last_col_letter}1")
    t_cell = ws["A1"]
    t_cell.value = f"HUMO TA'LIM MARKAZI — {group.name.upper()} GURUHI DAVOMAT JURNALI"
    t_cell.font = font_title
    t_cell.fill = fill_title
    t_cell.alignment = align_center
    ws.row_dimensions[1].height = 34

    # 2. Quyi Sarlavha / Meta-ma'lumotlar (Row 2)
    ws.merge_cells(f"A2:{last_col_letter}2")
    sub_cell = ws["A2"]
    sub_cell.value = (
        f"Fan: {group.subject or group.name}  |  "
        f"O'qituvchi: {group.teacher or '—'}  |  "
        f"Oylik davr: {curr_month}  |  "
        f"Guruh umumiy davomati: {group_pct:.0f}% ({group_total_present}/{group_total_records})  |  "
        f"Jami o'quvchilar: {len(students)} ta  |  "
        f"Eksport sanasi: {date.today().strftime('%d.%m.%Y')}"
    )
    sub_cell.font = font_sub
    sub_cell.fill = fill_sub
    sub_cell.alignment = align_center
    ws.row_dimensions[2].height = 24

    # 3. Jadval Sarlavhalari (Row 3)
    ws.row_dimensions[3].height = 28
    headers = [
        "№", "F.I.O", "Telefon raqami", "Kelgan sana",
        "To'lov tarifi", "Davomat foizi", "Keldi / Jami"
    ]
    # Har bir kun uchun ustun (01.11, 02.11, ..., 30.11)
    date_cols_meta = []
    for d in range(1, num_days + 1):
        cur_d = date(year, month, d)
        is_wknd = cur_d.weekday() in (5, 6)
        label = f"{d:02d}.{month:02d}"
        headers.append(label)
        date_cols_meta.append({'day': d, 'is_weekend': is_wknd, 'label': label})

    for col_idx, h in enumerate(headers, 1):
        c = ws.cell(row=3, column=col_idx, value=h)
        c.font = font_hdr
        c.alignment = align_center
        c.border = header_border
        if col_idx > 7 and date_cols_meta[col_idx - 8]['is_weekend']:
            c.fill = fill_hdr_weekend
        else:
            c.fill = fill_hdr

    # 4. O'quvchilar Qatorlari (Row 4..N)
    for idx, stat in enumerate(student_stats, 1):
        row_idx = 3 + idx
        ws.row_dimensions[row_idx].height = 20
        s = stat['student']
        p_cnt = stat['present_count']
        tot = stat['total_marked']
        pct = stat['percentage']
        s_att = stat['attendance_map']

        phone = f"{s.phone_1} {s.phone_2 or ''}".strip()
        joined = s.joined_date.strftime('%Y-%m-%d') if s.joined_date else ""
        rate_val = f"{s.payment_rate:,}".replace(',', ' ') if s.payment_rate else ""
        pct_display = f"{pct:.0f}%"
        ratio_display = f"{p_cnt}/{tot}"

        is_zebra = (idx % 2 == 0)
        base_fill = fill_zebra if is_zebra else None

        row_values = [
            (idx, align_center, font_cell, base_fill),
            (f"{s.first_name} {s.last_name}", align_left, font_bold, base_fill),
            (phone, align_center, font_cell, base_fill),
            (joined, align_center, font_cell, base_fill),
            (rate_val, align_right, font_cell, base_fill),
            (pct_display, align_center, font_bold, base_fill),
            (ratio_display, align_center, font_cell, base_fill),
        ]

        for col_idx, (val, algn, fnt, fll) in enumerate(row_values, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.alignment = algn
            cell.font = fnt
            cell.border = cell_border
            if fll:
                cell.fill = fll

        # Kunlik davomat kataklari (+, - yoki bo'sh)
        for d_meta in date_cols_meta:
            col_idx = 7 + d_meta['day']
            st = s_att.get(d_meta['day'], '')
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.border = cell_border
            cell.alignment = align_center

            if st == 'present':
                cell.value = "+"
                cell.font = font_present
                cell.fill = fill_present
            elif st == 'absent':
                cell.value = "-"
                cell.font = font_absent
                cell.fill = fill_absent
            else:
                cell.value = ""
                if base_fill:
                    cell.fill = base_fill

    # 5. Jami / Xulosa Qatori (Summary Row)
    summary_row_idx = 4 + len(student_stats)
    ws.row_dimensions[summary_row_idx].height = 24

    ws.cell(row=summary_row_idx, column=1, value="").border = total_border
    sum_title_cell = ws.cell(row=summary_row_idx, column=2, value="JAMI / GURUH DAVOMATI:")
    sum_title_cell.font = font_bold
    sum_title_cell.alignment = align_left
    sum_title_cell.fill = fill_total
    sum_title_cell.border = total_border

    for c_i in range(3, 6):
        c = ws.cell(row=summary_row_idx, column=c_i, value="")
        c.fill = fill_total
        c.border = total_border

    sum_pct_cell = ws.cell(row=summary_row_idx, column=6, value=f"{group_pct:.0f}%")
    sum_pct_cell.font = font_bold
    sum_pct_cell.alignment = align_center
    sum_pct_cell.fill = fill_total
    sum_pct_cell.border = total_border

    sum_ratio_cell = ws.cell(row=summary_row_idx, column=7, value=f"{group_total_present}/{group_total_records}")
    sum_ratio_cell.font = font_bold
    sum_ratio_cell.alignment = align_center
    sum_ratio_cell.fill = fill_total
    sum_ratio_cell.border = total_border

    # Har bir kunda nechta o'quvchi kelganligi (kunlik yig'indi)
    for d_meta in date_cols_meta:
        col_idx = 7 + d_meta['day']
        day_p_cnt = sum(
            1 for stat in student_stats
            if stat['attendance_map'].get(d_meta['day']) == 'present'
        )
        c = ws.cell(
            row=summary_row_idx,
            column=col_idx,
            value=day_p_cnt if day_p_cnt > 0 else ""
        )
        c.font = font_bold
        c.alignment = align_center
        c.fill = fill_total
        c.border = total_border

    # Ustun kengliklari
    ws.column_dimensions['A'].width = 6
    ws.column_dimensions['B'].width = 26
    ws.column_dimensions['C'].width = 18
    ws.column_dimensions['D'].width = 14
    ws.column_dimensions['E'].width = 14
    ws.column_dimensions['F'].width = 15
    ws.column_dimensions['G'].width = 14
    for d in range(1, num_days + 1):
        col_letter = get_column_letter(7 + d)
        ws.column_dimensions[col_letter].width = 6

    # Qator va ustunlarni muzlatish (Freeze panes)
    ws.freeze_panes = "H4"

    return wb


@admin_required
def admin_group_export_excel(request, group_id):
    """
    Guruh o'quvchilarining tanlangan oy bo'yicha yo'qlama (davomat) jurnali
    va shaxsiy ko'rsatkichlarini Excel fayl qilib yuklab berish.
    """
    if not is_admin_or_manager(request):
        return redirect('home')

    group = get_object_or_404(Group, id=group_id)
    curr_month, req_year, req_mon = normalize_month_str(request.GET.get('month'))

    wb = generate_group_attendance_excel(group, req_year, req_mon)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    safe_name = "".join(c for c in group.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
    response['Content-Disposition'] = f'attachment; filename="Davomat_{safe_name}_{curr_month}.xlsx"'
    wb.save(response)
    return response


@csrf_exempt
def public_student_payment(request, token):
    """
    Ochiq (public) sahifa: O'quvchining shaxsiy to'lov ma'lumotlarini ko'rish va to'lov qilish.
    URL: /pay/<uuid:token>/
    Har bir havola unikal UUIDField ga ega bo'lib, guess-resistant hisoblanadi.
    1. Avval PaymentRequest modeli orqali tekshiriladi.
    2. Agar topilmasa, Student.uuid orqali topilib, o'sha oy uchun PaymentRequest ulanadi.
    """
    from .models import PaymentRequest

    today = date.today()
    current_month = today.strftime('%Y-%m')

    # 1. Avval PaymentRequest bo'yicha tekshirish (unikal to'lov so'rovi)
    payment_request = PaymentRequest.objects.filter(uuid=token).select_related(
        'student__time_slot__group__teacher'
    ).first()

    if payment_request:
        student = payment_request.student
        current_month = payment_request.month or current_month
    else:
        # 2. Agar Student.uuid bo'lsa (orqaga moslik uchun)
        student = get_object_or_404(Student, uuid=token, is_deleted=False)
        payment_request = PaymentRequest.get_or_create_for_student(student, month_str=current_month)

    try:
        y, m = map(int, current_month.split('-'))
        curr_month_uz = f"{get_month_name_uz(m)} {y}"
    except Exception:
        curr_month_uz = f"{get_month_name_uz(today.month)} {today.year}"

    group = student.time_slot.group if (student.time_slot and student.time_slot.group) else None
    teacher = group.teacher if group else None

    # Oylik to'lov holatini tekshirish
    monthly_payment = MonthlyPayment.objects.filter(
        student=student,
        month=current_month,
    ).first()

    raw_paid, num_paid, has_text, is_paid = _payment_paid_info(monthly_payment)
    is_mahalla = _is_mahalla_student(student, monthly_payment)

    rate_val = payment_request.amount or student.payment_rate or 0
    if is_mahalla:
        due_amount = 0
        status_label = "Imtiyozli (Mahalla)"
        status_code = "mahalla"
    elif is_paid:
        due_amount = 0
        status_label = "To'langan"
        status_code = "paid"
    else:
        due_amount = max(0, rate_val - num_paid)
        status_label = "To'lanmagan (Qarzdor)"
        status_code = "unpaid"

    # Telegram chek havolasini shakllantirish (.env dagi TELEGRAM_RECEIPT_URL asosida)
    raw_tg = getattr(settings, 'TELEGRAM_RECEIPT_URL', 'https://t.me/humo_admin').strip()
    if raw_tg.startswith('@'):
        tg_base = f"https://t.me/{raw_tg[1:]}"
    elif not raw_tg.startswith('http'):
        tg_base = f"https://t.me/{raw_tg}"
    else:
        tg_base = raw_tg

    tg_msg = (
        f"Assalomu alaykum! {student.first_name} {student.last_name} "
        f"({curr_month_uz}) oyi uchun to'lov cheki."
    )
    if '?' in tg_base:
        telegram_url = tg_base
    else:
        telegram_url = f"{tg_base}?text={quote_plus(tg_msg)}"

    is_ajax_req = _is_ajax(request)

    # ─────────────────────────────────────────────────────────────
    # POST: Ota-ona yoki foydalanuvchi to'lovni tasdiqlaganda/yuborganda
    # ─────────────────────────────────────────────────────────────
    if request.method == 'POST':
        amount_raw = str(request.POST.get('amount') or request.POST.get('amount_paid') or '').strip()
        payment_type = request.POST.get('payment_type', 'click').strip().lower()
        if payment_type not in ('payme', 'click', 'terminal', 'cash'):
            payment_type = 'click'

        amount_num = _clean_amount(amount_raw)
        if amount_num <= 0:
            amount_num = due_amount if due_amount > 0 else rate_val

        # 1. PaymentRequest holatini saqlash (status: submitted)
        notes_text = f"Onlayn to'lov ({payment_type.upper()}): {amount_num} so'm. Chek kutilmoqda."
        payment_request.submitted_amount = amount_num
        payment_request.amount_paid = amount_num
        payment_request.payment_type = payment_type
        payment_request.status = 'submitted'
        payment_request.submitted_at = timezone.now()
        payment_request.receipt_note = notes_text
        payment_request.parent_phone = student.phone_1 or student.phone_2 or payment_request.parent_phone
        if not payment_request.group and group:
            payment_request.group = group
        payment_request.save()

        # 2. MonthlyPayment holatini belgilash va saqlash
        new_status = 'paid' if (not rate_val or amount_num >= rate_val) else 'partial'
        mp, created = MonthlyPayment.objects.update_or_create(
            student=student,
            month=current_month,
            defaults={
                'amount_paid': str(amount_num),
                'payment_type': payment_type,
                'payment_date': today,
                'status': new_status,
                'notes': notes_text,
            }
        )

        # 3. AuditLog ga qayd etish
        try:
            AuditLog.objects.create(
                action_type='payment',
                actor_name=f"Ota-ona ({student.first_name} {student.last_name})",
                title=f"Onlayn to'lov topshirildi: {student.first_name} {student.last_name} ({_fmt_money(amount_num)} so'm)",
                details=f"Guruh: {group.name if group else '-'}, Oy: {current_month}, Turi: {payment_type.upper()}, So'rov UUID: {payment_request.uuid}",
            )
        except Exception:
            pass

        if is_ajax_req:
            return JsonResponse({
                'success': True,
                'status': 'submitted',
                'payment_request_status': 'awaiting_receipt',
                'amount': amount_num,
                'amount_formatted': _fmt_money(amount_num),
                'payment_type': payment_type,
                'telegram_receipt_url': telegram_url,
                'telegram_url': telegram_url,
                'message': "To'lov muvaffaqiyatli qabul qilindi!",
            })

        # Oddiy POST bo'lsa
        context = {
            'student': student,
            'payment_request': payment_request,
            'group': group,
            'teacher': teacher,
            'current_month': current_month,
            'current_month_uz': curr_month_uz,
            'rate_val': rate_val,
            'rate_formatted': _fmt_money(rate_val),
            'paid_amount': amount_num,
            'paid_formatted': _fmt_money(amount_num),
            'due_amount': max(0, rate_val - amount_num),
            'due_amount_formatted': _fmt_money(max(0, rate_val - amount_num)),
            'half_amount': max(1000, (due_amount or rate_val) // 2),
            'status_code': new_status,
            'status_label': "To'langan" if new_status == 'paid' else "Qisman to'langan",
            'is_paid': new_status == 'paid',
            'is_mahalla': is_mahalla,
            'is_submitted': True,
            'submitted_amount': amount_num,
            'submitted_amount_formatted': _fmt_money(amount_num),
            'submitted_type': payment_type,
            'telegram_receipt_url': telegram_url,
            'telegram_url': telegram_url,
            'telegram_base': tg_base,
            'center_name': getattr(settings, 'CENTER_NAME', "Humo Ta'lim o'quv markazi"),
            'center_phone': getattr(settings, 'CENTER_PHONE', '+998 90 123 45 67'),
        }
        return render(request, 'public_payment.html', context)

    # ─────────────────────────────────────────────────────────────
    # GET: Sahifani yuklash
    # ─────────────────────────────────────────────────────────────
    half_amount = max(1000, (due_amount or rate_val) // 2)
    has_submitted = bool(
        payment_request.status in ('awaiting_receipt', 'submitted', 'confirmed')
        or num_paid > 0
        or is_paid
    )
    submitted_amt = payment_request.amount_paid if payment_request.amount_paid > 0 else (
        num_paid if num_paid > 0 else (rate_val if is_paid else due_amount)
    )

    context = {
        'student': student,
        'payment_request': payment_request,
        'group': group,
        'teacher': teacher,
        'current_month': current_month,
        'current_month_uz': curr_month_uz,
        'rate_val': rate_val,
        'rate_formatted': _fmt_money(rate_val),
        'paid_amount': num_paid,
        'paid_formatted': _fmt_money(num_paid),
        'due_amount': due_amount,
        'due_amount_formatted': _fmt_money(due_amount),
        'half_amount': half_amount,
        'status_code': status_code,
        'status_label': status_label,
        'is_paid': is_paid or is_mahalla or (due_amount == 0 and num_paid > 0),
        'is_mahalla': is_mahalla,
        'is_submitted': has_submitted,
        'submitted_amount': submitted_amt,
        'submitted_amount_formatted': _fmt_money(submitted_amt),
        'submitted_type': payment_request.payment_type if payment_request else (monthly_payment.payment_type if monthly_payment else 'click'),
        'telegram_receipt_url': telegram_url,
        'telegram_url': telegram_url,
        'telegram_base': tg_base,
        'center_name': getattr(settings, 'CENTER_NAME', "Humo Ta'lim o'quv markazi"),
        'center_phone': getattr(settings, 'CENTER_PHONE', '+998 90 123 45 67'),
    }
    return render(request, 'public_payment.html', context)


@admin_required
def admin_payment_requests_monitoring(request):
    """
    Admin va Menejerlar uchun onlayn to'lov so'rovlari va Telegram cheklarini tekshirish sahifasi.
    Ko'rsatadi:
      - O'quvchi ismi, guruhi, hisob oyi
      - Kiritilgan to'lov summasi
      - Joriy holat va topshirilgan vaqt
      - Telegram orqali kelayotgan cheklarni kiritilgan summalar bilan oson solishtirish va tasdiqlash.
    """
    from .models import PaymentRequest, Group
    from django.db.models import Sum

    if not is_admin_or_manager(request):
        messages.error(request, "Ushbu sahifaga kirish uchun ruxsat yo'q!")
        return redirect('home')

    today = date.today()
    selected_month = request.GET.get('month', '').strip()
    selected_status = request.GET.get('status', '').strip()
    selected_group = request.GET.get('group_id', '').strip()
    search_q = request.GET.get('q', '').strip()

    qs = PaymentRequest.objects.select_related(
        'student__time_slot__group__teacher',
        'group__teacher',
        'confirmed_by'
    ).order_by('-submitted_at', '-created_at')

    if selected_month:
        qs = qs.filter(month=selected_month)
    if selected_status and selected_status != 'all':
        qs = qs.filter(status=selected_status)
    if selected_group and selected_group.isdigit():
        qs = qs.filter(Q(group_id=int(selected_group)) | Q(student__time_slot__group_id=int(selected_group)))
    if search_q:
        qs = qs.filter(
            Q(student__first_name__icontains=search_q) |
            Q(student__last_name__icontains=search_q) |
            Q(student__phone_1__icontains=search_q) |
            Q(parent_phone__icontains=search_q) |
            Q(receipt_note__icontains=search_q) |
            Q(uuid__icontains=search_q)
        )

    # Statistika hisoblash
    all_requests = PaymentRequest.objects.all()
    if selected_month:
        all_requests = all_requests.filter(month=selected_month)

    total_count = all_requests.count()
    awaiting_count = all_requests.filter(status__in=['awaiting_receipt', 'submitted']).count()
    confirmed_count = all_requests.filter(status='confirmed').count()
    total_sum_val = all_requests.filter(status__in=['awaiting_receipt', 'submitted', 'confirmed']).aggregate(
        total=Sum('amount_paid')
    )['total'] or 0

    # Mavjud oylar ro'yxati (oxirgi 12 oy)
    months_list = []
    for i in range(12):
        if relativedelta:
            m_date = today - relativedelta(months=i)
        else:
            yr = today.year
            mn = today.month - i
            while mn <= 0:
                mn += 12
                yr -= 1
            m_date = date(yr, mn, 1)
        m_str = m_date.strftime('%Y-%m')
        m_name = f"{get_month_name_uz(m_date.month)} {m_date.year}"
        months_list.append({'val': m_str, 'name': m_name})

    groups = Group.objects.all().order_by('name')

    context = {
        'payment_requests': qs[:200],
        'total_count': total_count,
        'awaiting_count': awaiting_count,
        'confirmed_count': confirmed_count,
        'total_sum_formatted': _fmt_money(total_sum_val),
        'selected_month': selected_month,
        'selected_status': selected_status,
        'selected_group': int(selected_group) if selected_group.isdigit() else None,
        'search_q': search_q,
        'months_list': months_list,
        'groups': groups,
        'status_choices': PaymentRequest.STATUS_CHOICES,
    }
    return render(request, 'admin_payment_monitoring.html', context)


@admin_required
def admin_confirm_payment_request(request, request_id):
    """Menejer yoki Admin tomonidan to'lov chekini tasdiqlash."""
    from .models import PaymentRequest
    if not is_admin_or_manager(request):
        return JsonResponse({'status': 'error', 'message': "Ruxsat yo'q"}, status=403)

    pr = get_object_or_404(PaymentRequest, id=request_id)
    pr.status = 'confirmed'
    pr.confirmed_at = timezone.now()
    pr.confirmed_by = request.user
    pr.save(update_fields=['status', 'confirmed_at', 'confirmed_by'])

    # MonthlyPayment ni ham to'langan qilish
    paid_val = str(pr.amount_paid or pr.amount)
    MonthlyPayment.objects.update_or_create(
        student=pr.student,
        month=pr.month,
        defaults={
            'amount_paid': paid_val,
            'payment_type': pr.payment_type,
            'payment_date': date.today(),
            'status': 'paid',
            'notes': f"Onlayn to'lov ({pr.payment_type.upper()}) tasdiqlandi: {paid_val} so'm",
        }
    )

    try:
        AuditLog.objects.create(
            action_type='payment',
            actor=request.user,
            actor_name=f"Xodim ({request.user.get_full_name() or request.user.username})",
            title=f"Onlayn to'lov cheki tasdiqlandi: {pr.student.first_name} {pr.student.last_name} ({paid_val} so'm)",
            details=f"Oy: {pr.month}, Turi: {pr.payment_type}, UUID: {pr.uuid}",
        )
    except Exception:
        pass

    if _is_ajax(request):
        return JsonResponse({'status': 'success', 'message': "To'lov cheki muvaffaqiyatli tasdiqlandi!"})
    messages.success(request, f"{pr.student.first_name} {pr.student.last_name} to'lovi muvaffaqiyatli tasdiqlandi!")
    return redirect(request.META.get('HTTP_REFERER') or 'admin_payment_requests_monitoring')


@admin_required
def admin_reject_payment_request(request, request_id):
    """Menejer yoki Admin tomonidan to'lov chekini rad etish."""
    from .models import PaymentRequest
    if not is_admin_or_manager(request):
        return JsonResponse({'status': 'error', 'message': "Ruxsat yo'q"}, status=403)

    pr = get_object_or_404(PaymentRequest, id=request_id)
    pr.status = 'rejected'
    pr.save(update_fields=['status'])

    try:
        AuditLog.objects.create(
            action_type='payment',
            actor=request.user,
            actor_name=f"Xodim ({request.user.get_full_name() or request.user.username})",
            title=f"Onlayn to'lov cheki rad etildi: {pr.student.first_name} {pr.student.last_name}",
            details=f"Oy: {pr.month}, UUID: {pr.uuid}",
        )
    except Exception:
        pass

    if _is_ajax(request):
        return JsonResponse({'status': 'success', 'message': "To'lov rad etildi!"})
    messages.warning(request, f"{pr.student.first_name} {pr.student.last_name} to'lovi rad etildi.")
    return redirect(request.META.get('HTTP_REFERER') or 'admin_payment_requests_monitoring')


@admin_required
def admin_send_test_sms(request):
    """
    Faqat .env dagi TEST_SMS_PHONE ga sinov to'lov eslatmasi (shaxsiy to'lov havolasi bilan) yuborish.
    """
    is_ajax_req = _is_ajax(request)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'status': 'error', 'message': "Faqat POST so'rovi qabul qilinadi."}, status=405)

    try:
        month_str = request.POST.get('month', '').strip()
        student_id = request.POST.get('student_id', '').strip()
        group_id = request.POST.get('group_id', '').strip()
        custom_message = request.POST.get('custom_message', '').strip()
        if not month_str:
            month_str = date.today().strftime('%Y-%m')

        from .eskiz_service import send_test_payment_reminder
        res = send_test_payment_reminder(
            student_id=student_id if student_id.isdigit() else None,
            month_str=month_str,
            sent_by=request.user,
            custom_message=custom_message or None,
            group_id=int(group_id) if group_id.isdigit() else None,
        )

        if res.get('success'):
            msg = (
                f"Sinov SMS muvaffaqiyatli yuborildi! "
                f"Test raqam: +{res['test_phone']}. "
                f"O'quvchi: {res['student_name']}. "
                f"To'lov havolasi: {res['payment_url']}"
            )
            if is_ajax_req:
                return JsonResponse({'success': True, 'status': 'success', 'message': msg, 'result': res})
            messages.success(request, msg)
        else:
            err_msg = res.get('error', 'Nomaʼlum xatolik')
            msg = f"Sinov SMS yuborishda xatolik: {err_msg}"
            if is_ajax_req:
                return JsonResponse({'success': False, 'status': 'error', 'message': msg, 'error': err_msg, 'result': res})
            messages.error(request, msg)

        next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/admin/payments/'
        return redirect(next_url)

    except Exception as exc:
        logger.exception("Sinov SMS yuborishda server xatoligi: %s", exc)
        err_msg = f"Server xatoligi yuz berdi: {str(exc)}"
        if is_ajax_req:
            return JsonResponse({'success': False, 'status': 'error', 'message': err_msg, 'error': str(exc)})
        messages.error(request, err_msg)
        next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/admin/payments/'
        return redirect(next_url)


@admin_required
def admin_send_payment_reminders(request):
    """
    Admin yoki Menejer paneli orqali to'lov eslatmasi SMS xabarlarini yuborish:
    1. action='test' -> Faqat .env dagi TEST_SMS_PHONE ga sinov SMS yuboradi.
    2. action='bulk' (yoki standart) -> Barcha to'lovli faol o'quvchilarga ommaviy yuboradi,
       Mahalla / Imtiyozli o'quvchilar mutlaqo chiqarib tashlanadi va ularga SMS yuborilmaydi.
    """
    is_ajax_req = _is_ajax(request)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'status': 'error', 'message': "Faqat POST so'rovi qabul qilinadi."}, status=405)

    try:
        action = request.POST.get('action', 'bulk').strip().lower()
        month_str = request.POST.get('month', '').strip()
        group_id = request.POST.get('group_id', '').strip()
        student_id = request.POST.get('student_id', '').strip()
        custom_message = request.POST.get('custom_message', '').strip()
        force = request.POST.get('force') in ('1', 'true', 'True')
        dry_run = request.POST.get('dry_run') in ('1', 'true', 'True')

        if not month_str:
            month_str = date.today().strftime('%Y-%m')

        from .eskiz_service import send_payment_reminders_to_paid_students, send_test_payment_reminder

        # 1. TEST REJIMI
        if action in ('test', 'test_sms'):
            res = send_test_payment_reminder(
                student_id=student_id if student_id.isdigit() else None,
                month_str=month_str,
                sent_by=request.user,
                custom_message=custom_message or None,
                group_id=int(group_id) if group_id.isdigit() else None,
            )
            if res.get('success'):
                msg = (
                    f"Sinov SMS muvaffaqiyatli yuborildi! "
                    f"Test raqam: +{res['test_phone']}. "
                    f"O'quvchi: {res['student_name']}. "
                    f"To'lov havolasi: {res['payment_url']}"
                )
                if is_ajax_req:
                    return JsonResponse({'success': True, 'status': 'success', 'message': msg, 'result': res})
                messages.success(request, msg)
            else:
                err_msg = res.get('error', 'Nomaʼlum xatolik')
                msg = f"Sinov SMS yuborishda xatolik: {err_msg}"
                if is_ajax_req:
                    return JsonResponse({'success': False, 'status': 'error', 'message': msg, 'error': err_msg, 'result': res})
                messages.error(request, msg)

            next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/admin/payments/'
            return redirect(next_url)

        # 2. BULK (OMMAVIY) REJIM: Faqat to'lovli qarzdor o'quvchilar (Mahalla o'quvchilari chiqarib tashlanadi)
        summary = send_payment_reminders_to_paid_students(
            month_str=month_str,
            group_id=int(group_id) if group_id.isdigit() else None,
            dry_run=dry_run,
            force=force,
            sent_by=request.user,
            custom_message=custom_message or None,
            debtors_only=True,
        )

        total = summary.get('total_paid_students', 0)
        sent = summary.get('sent', 0)
        skipped = summary.get('skipped', 0)
        skipped_mahalla = summary.get('skipped_mahalla', 0)
        failed = summary.get('failed', 0)

        grp_obj = None
        if group_id and group_id.isdigit():
            from .models import Group
            grp_obj = Group.objects.filter(id=int(group_id)).first()
        grp_prefix = f"\"{grp_obj.name}\" guruhi bo'yicha: " if grp_obj else ""

        msg = (
            f"{grp_prefix}{summary['month']} oyi uchun jami {total} ta to'lovli qarzdor tekshirildi. "
            f"{sent} ta ota-onaga shaxsiy to'lov havolasi bilan SMS eslatma yuborildi."
        )
        if skipped_mahalla > 0:
            msg += f" {skipped_mahalla} ta mahalla/imtiyozli o'quvchi o'tkazib yuborildi."
        if skipped > 0:
            msg += f" {skipped} ta o'quvchiga avval yuborilganligi uchun o'tkazib yuborildi."
        if failed > 0:
            msg += f" {failed} ta raqamga yuborishda xatolik yuz berdi."

        has_errors = (sent == 0 and total > 0 and failed > 0)
        first_error = ""
        for det in summary.get('details', []):
            if det.get('error') and det.get('status') not in ('skipped', 'skipped_mahalla'):
                first_error = det.get('error')
                break

        if has_errors and first_error:
            msg += f" (Xatolik sababi: {first_error})"

        is_successful = sent > 0 or (total == 0 and not failed)
        if is_ajax_req:
            return JsonResponse({
                'success': is_successful,
                'status': 'success' if is_successful else 'error',
                'message': msg,
                'summary': summary,
                'error': first_error if has_errors else None,
            })

        if has_errors:
            messages.warning(request, msg)
        else:
            messages.success(request, msg)

        next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/admin/payments/'
        return redirect(next_url)

    except Exception as exc:
        logger.exception("SMS eslatma yuborishda server xatoligi: %s", exc)
        err_msg = f"Serverda xatolik yuz berdi: {str(exc)}"
        if is_ajax_req:
            return JsonResponse({
                'success': False,
                'status': 'error',
                'message': err_msg,
                'error': str(exc)
            })
        messages.error(request, err_msg)
        next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/admin/payments/'
        return redirect(next_url)



