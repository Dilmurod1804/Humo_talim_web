"""
HUMO O'quv markazi — Oylik Davomat Hisob-kitobi va O'qituvchi Maoshi Integratsiyasi (attendance_service.py)

Ushbu modul:
1. Yakka o'quvchining oylik davomat foizini hisoblaydi:
   Formula: (Kelgan darslar soni / O'sha oyda o'tkazilgan jami darslar) * 100
2. Muayyan guruhning oylik umumiy davomat foizini hisoblaydi:
   Formula: (Guruhdagi jami kelganlik qaydlari / Guruhdagi jami yo'qlama qaydlari) * 100
3. O'qituvchining barcha guruhlari bo'yicha oylik umumiy davomatini umumlashtiradi va
   o'qituvchi oylik maoshi (KPI / Bonus / Rag'batlantirish) bilan integratsiya qiladi.
"""
from decimal import Decimal
import logging
from django.db.models import Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 1. TALABANING OYLIK DAVOMAT FOIZI
# ─────────────────────────────────────────────────────────────

def compute_student_attendance_stats(attendance_day_map: dict) -> dict:
    """
    O'quvchining kunlik davomat lug'ati ({day: status}) asosida oylik
    kelgan/kelmagan/jami belgilangan darslar soni va davomat foizini hisoblaydi.

    Agar birorta ham haqiqiy belgilangan dars bo'lmasa (total_marked == 0),
    davomat foizi qat'iy ravishda 0.0 qaytariladi (100% emas).
    """
    present_count = sum(1 for st in attendance_day_map.values() if st == 'present')
    absent_count = sum(1 for st in attendance_day_map.values() if st == 'absent')
    total_marked = present_count + absent_count

    percentage = round((present_count / total_marked) * 100, 1) if total_marked > 0 else 0.0

    return {
        'present_count': present_count,
        'absent_count': absent_count,
        'total_count': total_marked,
        'percentage': percentage,
        'has_records': total_marked > 0,
    }


def calculate_student_monthly_attendance(student_or_id, year: int, month: int) -> dict:
    """
    Muayyan o'quvchining berilgan yil va oy uchun davomat foizini hisoblaydi.

    Formula:
      (Kelgan darslar / Jami o'tkazilgan darslar) * 100

    Natija:
      {
          'student_id': int,
          'student_name': str,
          'year': int,
          'month': int,
          'month_str': 'YYYY-MM',
          'present_classes': int,       # Kelgan darslar soni (+)
          'absent_classes': int,        # Qoldirilgan darslar soni (-)
          'total_classes': int,         # Jami o'tkazilgan darslar (yo'qlama qilingan kunlar)
          'percentage': float,          # 0.0 dan 100.0 gacha (2 xona aniqlikda)
          'has_records': bool,
      }
    """
    from core.models import Student, Attendance

    if isinstance(student_or_id, Student):
        student = student_or_id
    else:
        student = Student.objects.select_related('time_slot__group').get(id=student_or_id)

    qs = Attendance.objects.filter(
        student=student,
        date__year=year,
        date__month=month,
        status__in=['present', 'absent'],
    )

    stats = qs.aggregate(
        present=Count('id', filter=Q(status='present')),
        absent=Count('id', filter=Q(status='absent')),
    )

    present_classes = stats['present'] or 0
    absent_classes = stats['absent'] or 0
    total_classes = present_classes + absent_classes

    # Dars o'tilmagan yoki yo'qlama qilinmagan bo'lsa 0.0 (hech qachon 100% emas)
    percentage = (
        round((present_classes / total_classes) * 100, 2)
        if total_classes > 0 else 0.0
    )

    return {
        'student_id': student.id,
        'student_name': f"{student.first_name} {student.last_name}".strip(),
        'year': year,
        'month': month,
        'month_str': f"{year}-{month:02d}",
        'present_classes': present_classes,
        'absent_classes': absent_classes,
        'total_classes': total_classes,
        'percentage': percentage,
        'has_records': total_classes > 0,
    }


# ─────────────────────────────────────────────────────────────
# 2. GURUHNING OYLIK UMUMIY DAVOMAT FOIZI
# ─────────────────────────────────────────────────────────────

def calculate_group_monthly_attendance(group_or_id, year: int, month: int) -> dict:
    """
    Muayyan guruhning berilgan yil va oy uchun umumiy davomat ko'rsatkichini hisoblaydi.

    Formula:
      (Guruhdagi jami kelganlik qaydlari / Guruhdagi jami yo'qlama qaydlari) * 100

    Natija:
      {
          'group_id': int,
          'group_name': str,
          'subject': str,
          'teacher_id': int | None,
          'teacher_name': str | None,
          'year': int,
          'month': int,
          'month_str': 'YYYY-MM',
          'total_records': int,         # Jami yo'qlama qaydlari
          'present_records': int,       # Jami kelganlar soni
          'absent_records': int,        # Jami kelmaganlar soni
          'percentage': float,          # Guruh davomat foizi (0.0 - 100.0)
          'students_count': int,        # Yo'qlama qilingan noyob o'quvchilar soni
          'lesson_days_count': int,     # Yo'qlama qilingan noyob dars kunlari soni
          'has_records': bool,
      }
    """
    from core.models import Group, Attendance

    if isinstance(group_or_id, Group):
        group = group_or_id
    else:
        group = Group.objects.select_related('teacher').get(id=group_or_id)

    # Guruhga tegishli barcha o'quvchilarning shu oydagi davomat qaydlari (faqat present/absent)
    qs = Attendance.objects.filter(
        student__time_slot__group=group,
        date__year=year,
        date__month=month,
        status__in=['present', 'absent'],
    )

    stats = qs.aggregate(
        present=Count('id', filter=Q(status='present')),
        absent=Count('id', filter=Q(status='absent')),
    )

    present_records = stats['present'] or 0
    absent_records = stats['absent'] or 0
    total_records = present_records + absent_records

    percentage = (
        round((present_records / total_records) * 100, 2)
        if total_records > 0 else 0.0
    )

    distinct_students = qs.values('student').distinct().count()
    distinct_dates = qs.values('date').distinct().count()

    teacher_name = None
    if group.teacher:
        teacher_name = f"{group.teacher.first_name} {group.teacher.last_name}".strip()

    return {
        'group_id': group.id,
        'group_name': group.name,
        'subject': getattr(group, 'subject_name', group.name),
        'teacher_id': group.teacher_id,
        'teacher_name': teacher_name,
        'year': year,
        'month': month,
        'month_str': f"{year}-{month:02d}",
        'total_records': total_records,
        'present_records': present_records,
        'absent_records': absent_records,
        'percentage': percentage,
        'students_count': distinct_students,
        'lesson_days_count': distinct_dates,
        'has_records': total_records > 0,
    }


# ─────────────────────────────────────────────────────────────
# 3. O'QITUVCHINING BARCHA GURUHLARI BO'YICHA DAVOMAT STATISTIKASI
# ─────────────────────────────────────────────────────────────

def calculate_teacher_monthly_attendance(teacher_or_id, year: int, month: int) -> dict:
    """
    O'qituvchining barcha guruhlari bo'yicha berilgan oy uchun umumiy davomatini hisoblaydi.
    Har bir guruhning alohida ko'rsatkichlari va o'qituvchining umumiy yig'ma foizini beradi.
    """
    from core.models import Teacher

    if isinstance(teacher_or_id, Teacher):
        teacher = teacher_or_id
    else:
        teacher = Teacher.objects.prefetch_related('groups').get(id=teacher_or_id)

    groups = list(teacher.groups.all())
    group_reports = []

    overall_total = 0
    overall_present = 0
    overall_absent = 0

    for grp in groups:
        grp_data = calculate_group_monthly_attendance(grp, year, month)
        group_reports.append(grp_data)
        overall_total += grp_data['total_records']
        overall_present += grp_data['present_records']
        overall_absent += grp_data['absent_records']

    overall_percentage = (
        round((overall_present / overall_total) * 100, 2)
        if overall_total > 0 else 0.0
    )

    return {
        'teacher_id': teacher.id,
        'teacher_name': f"{teacher.first_name} {teacher.last_name}".strip(),
        'year': year,
        'month': month,
        'month_str': f"{year}-{month:02d}",
        'groups_count': len(groups),
        'groups': group_reports,
        'total_records': overall_total,
        'present_records': overall_present,
        'absent_records': overall_absent,
        'overall_percentage': overall_percentage,
        'has_records': overall_total > 0,
    }


# ─────────────────────────────────────────────────────────────
# 4. O'QITUVCHI MAOSHI VA KPI INTEGRATSIYASI
# ─────────────────────────────────────────────────────────────

# KPI davomat mezonlari (standart sozlamalar)
ATTENDANCE_EXCELLENT_THRESHOLD = 90.0  # 90%+ bo'lsa a'lo daraja (Bonusga loyiq)
ATTENDANCE_GOOD_THRESHOLD = 80.0       # 80% - 89.9% yaxshi daraja (Standart maosh)
ATTENDANCE_WARNING_THRESHOLD = 70.0    # 70% dan past bo'lsa ogohlantirish / past ko'rsatkich
ATTENDANCE_BONUS_PERCENT = Decimal('5.0')  # Asosiy maoshdan 5% bonus stavkasi


def evaluate_teacher_attendance_kpi(
    teacher_or_id,
    year: int,
    month: int,
    bonus_threshold: float = ATTENDANCE_EXCELLENT_THRESHOLD,
    bonus_percent_rate: Decimal = ATTENDANCE_BONUS_PERCENT,
    fixed_bonus_amount: Decimal | None = None,
) -> dict:
    """
    O'qituvchining oylik davomati asosida KPI holati va taklif etilayotgan bonus summasini baholaydi.

    KPI mezonlari:
      - 'EXCELLENT' (>= 90%): A'lo daraja, o'qituvchiga maoshiga qo'shimcha rag'batlantirish bonusi hisoblanadi.
      - 'GOOD' (80% - 89.9%): Yaxshi / Mo''tadil daraja, reja bajarilgan.
      - 'AVERAGE' (70% - 79.9%): O'rtacha, nazorat talab etiladi.
      - 'POOR' (< 70%): Past davomat, e'tibor qaratilishi shart bo'lgan guruhlar mavjud.
      - 'NO_DATA': Darslar o'tilmagan yoki yo'qlama qaydlari mavjud emas.

    Bonus hisoblash varianti:
      Agar fixed_bonus_amount berilsa, qat'iy summa (masalan: 200 000 so'm);
      Aks holda o'qituvchining oylik stavkasidan bonus_percent_rate (masalan 5%) hisoblanadi.
    """
    from core.models import Teacher

    if isinstance(teacher_or_id, Teacher):
        teacher = teacher_or_id
    else:
        teacher = Teacher.objects.get(id=teacher_or_id)

    att_summary = calculate_teacher_monthly_attendance(teacher, year, month)
    pct = att_summary['overall_percentage']
    has_records = att_summary['has_records']

    base_salary = Decimal(str(getattr(teacher, 'monthly_salary', 0) or 0))
    suggested_bonus = Decimal('0.00')

    if not has_records:
        kpi_status = 'NO_DATA'
        badge_class = 'secondary'
        badge_text = "Ma'lumot yo'q"
        note = "Bu oyda guruhlar bo'yicha yo'qlama kiritilmagan."
    elif pct >= bonus_threshold:
        kpi_status = 'EXCELLENT'
        badge_class = 'success'
        badge_text = "A'lo davomat (+Bonus)"
        if fixed_bonus_amount is not None:
            suggested_bonus = Decimal(str(fixed_bonus_amount))
        elif base_salary > 0:
            suggested_bonus = round((base_salary * bonus_percent_rate) / Decimal('100.0'), 2)
        note = f"Guruhlar davomati yuqori ({pct}%). O'qituvchiga {bonus_percent_rate}% rag'batlantirish bonusi tavsiya etiladi."
    elif pct >= ATTENDANCE_GOOD_THRESHOLD:
        kpi_status = 'GOOD'
        badge_class = 'primary'
        badge_text = "Yaxshi davomat"
        note = f"Guruhlar davomati talabga javob beradi ({pct}%)."
    elif pct >= ATTENDANCE_WARNING_THRESHOLD:
        kpi_status = 'AVERAGE'
        badge_class = 'warning'
        badge_text = "O'rtacha davomat"
        note = f"Guruhlar davomati {pct}%. Davomatni oshirish tavsiya etiladi."
    else:
        kpi_status = 'POOR'
        badge_class = 'danger'
        badge_text = "Past davomat (Diqqat)"
        note = f"Guruhlar davomati belgilangan me'yordan past ({pct}%)."

    return {
        'teacher_id': teacher.id,
        'teacher_name': att_summary['teacher_name'],
        'year': year,
        'month': month,
        'month_str': att_summary['month_str'],
        'attendance_percentage': pct,
        'total_records': att_summary['total_records'],
        'present_records': att_summary['present_records'],
        'absent_records': att_summary['absent_records'],
        'groups': att_summary['groups'],
        'groups_count': att_summary['groups_count'],
        'has_records': has_records,
        'kpi_status': kpi_status,
        'badge_class': badge_class,
        'badge_text': badge_text,
        'note': note,
        'is_bonus_eligible': kpi_status == 'EXCELLENT',
        'suggested_bonus': suggested_bonus,
        'base_salary': base_salary,
    }


def enrich_salary_summary_with_attendance(salary_summary: dict, teacher, year: int, month: int) -> dict:
    """
    Mavjud `calculate_staff_salary_summary` ma'lumotlariga o'qituvchining guruhlar davomat
    tahlili va KPI ma'lumotlarini qo'shadi.
    Oylik maosh hisoboti va interfeysda to'liq ko'rsatish uchun xizmat qiladi.
    """
    kpi = evaluate_teacher_attendance_kpi(teacher, year, month)
    salary_summary['attendance_kpi'] = kpi
    salary_summary['attendance_percentage'] = kpi['attendance_percentage']
    salary_summary['attendance_badge_class'] = kpi['badge_class']
    salary_summary['attendance_badge_text'] = kpi['badge_text']
    salary_summary['attendance_suggested_bonus'] = kpi['suggested_bonus']
    return salary_summary


def apply_teacher_attendance_bonus(
    teacher,
    year: int,
    month: int,
    created_by=None,
    custom_amount: Decimal | None = None,
    custom_reason: str | None = None,
) -> tuple[object | None, bool, str]:
    """
    Yuqori davomatga erishgan o'qituvchiga ma'lumotlar bazasida haqiqiy `StaffBonus`
    qaydnomasini avtomatik yoki direktor tasdig'i bilan yaratadi.

    Takroriy qo'shilishdan himoyalangan (bitta oyda bitta sabab bilan ikkinchi marta yaratilmaydi).

    Qaytaradi: (bonus_instance, created: bool, message: str)
    """
    from core.models import StaffBonus, CustomUser, Teacher

    if isinstance(teacher, Teacher):
        teacher_user = teacher.user
    elif isinstance(teacher, CustomUser):
        teacher_user = teacher
        teacher = getattr(teacher, 'teacher_profile', None)
    else:
        teacher = Teacher.objects.get(id=teacher)
        teacher_user = teacher.user

    month_str = f"{year}-{month:02d}"

    kpi = evaluate_teacher_attendance_kpi(teacher, year, month)
    bonus_amount = custom_amount if custom_amount is not None else kpi['suggested_bonus']

    if bonus_amount <= 0:
        return None, False, f"O'qituvchi davomat ko'rsatkichi ({kpi['attendance_percentage']}%) bo'yicha bonusga ega emas."

    reason = custom_reason or f"Yuqori guruh davomati uchun KPI bonus ({kpi['attendance_percentage']}%)"

    # Allaqachon ushbu oyda davomat bonusi berilganligini tekshirish
    existing_bonus = StaffBonus.objects.filter(
        user=teacher_user,
        month=month_str,
        reason__icontains="davomat",
    ).first()

    if existing_bonus:
        return existing_bonus, False, f"{month_str} oyi uchun ushbu o'qituvchiga davomat bonusi allaqachon berilgan ({existing_bonus.amount} so'm)."

    bonus = StaffBonus.objects.create(
        user=teacher_user,
        month=month_str,
        amount=bonus_amount,
        reason=reason,
        created_by=created_by,
    )

    return bonus, True, f"{teacher.first_name} {teacher.last_name} ga {bonus_amount} so'm davomat bonusi muvaffaqiyatli biriktirildi."
