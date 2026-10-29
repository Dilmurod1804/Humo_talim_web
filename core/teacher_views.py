"""
HUMO O'quv markazi - Teacher Views
O'qituvchi paneli: jurnal, davomat, o'quvchilar, chat, Excel eksport.
"""
import os
import calendar
from datetime import date, datetime

try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    relativedelta = None

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.core.exceptions import ValidationError

import openpyxl

from .models import (
    Attendance, CustomUser, DeletedStudent, Group, Student,
    Teacher, TimeSlot, MonthlyPayment, Message
)
from .services import optimize_student_photo
from .validators import contains_cyrillic
from .auth_views import _admin_only
from .schedule_helper import parse_schedule_weekdays


# ─────────────────────────────────────────────
# Shared: o'quvchi jurnal filtri
# O'quvchi guruhga biriktirilgan ekan - hech qachon g'oyib bo'lmaydi.
# Faqat shu oyda left_date ham bo'lmagan (ketgan) o'quvchilar filtrlanadi.
# ─────────────────────────────────────────────

def _student_visible_in_month(st, year=None, month=None):
    """
    O'quvchi faqat faol bo'lsa (is_active=True) va ketgan sanasi bo'lmasa (left_date is None)
    jurnalda ko'rinadi. Arxivlangan/o'chirilgan o'quvchilar mutlaqo chiqarilmaydi.
    """
    return bool(getattr(st, 'is_active', False) and getattr(st, 'left_date', None) is None)


def _build_attendance_matrix(students, days_data, attendance_map):
    """Har bir o'quvchi uchun davomat matritsasini tuzadi."""
    for s in students:
        matrix = []
        for d in days_data:
            st = attendance_map.get(s.id, {}).get(d['day'], '')
            is_today = d.get('is_today', False)
            matrix.append({
                'day': d['day'],
                'date_str': d['date_str'],
                'is_lesson': d.get('is_lesson', False),
                'status': st,
                'is_today': is_today,
                'is_past': d.get('is_past', False),
                'is_future': d.get('is_future', False),
                'can_mark': d.get('is_lesson', False) and is_today,
            })
        s.attendance_matrix = matrix


def _make_days_data(year, month):
    """Berilgan oy uchun kunlar ro'yxatini tayyorlaydi."""
    num_days = calendar.monthrange(year, month)[1]
    weekdays_uz = ['Du', 'Se', 'Chor', 'Pay', 'Ju', 'Sha', 'Yak']
    today_date = timezone.now().date()
    days_data = []
    for d in range(1, num_days + 1):
        cur_d = date(year, month, d)
        w_idx = cur_d.weekday()
        days_data.append({
            'day': d,
            'weekday_idx': w_idx,
            'date_str': cur_d.strftime('%Y-%m-%d'),
            'display_day': f"{d:02d}",
            'display_date': cur_d.strftime('%d.%m'),
            'weekday': weekdays_uz[w_idx],
            'is_weekend': w_idx in (5, 6),
            'is_today': cur_d == today_date,
            'is_past': cur_d < today_date,
            'is_future': cur_d > today_date,
        })
    return days_data, num_days


# ─────────────────────────────────────────────
# Excel: shared helper
# ─────────────────────────────────────────────

def _populate_excel_sheet(ws, students, payment_map):
    """Excel varag'iga standart ustunlar va o'quvchilar ro'yxatini yozish."""
    headers = [
        "\u2116", "F.I.O", "Fan (Guruh)", "O'qituvchi", "Kelgan sana",
        "To'lov tari", "To'lov miq", "Telefon raqami"
    ]
    ws.append(headers)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF", size=11)
        cell.fill = openpyxl.styles.PatternFill(
            start_color="4F46E5", end_color="4F46E5", fill_type="solid")
        cell.alignment = openpyxl.styles.Alignment(
            horizontal="center", vertical="center")

    for idx, s in enumerate(students, 1):
        group_name = s.time_slot.group.name if (
            s.time_slot and s.time_slot.group) else "-"
        teacher_name = str(s.time_slot.group.teacher) if (
            s.time_slot and s.time_slot.group and s.time_slot.group.teacher) else "-"
        amount = payment_map.get(s.id, "")
        phone = f"{s.phone_1} {s.phone_2 or ''}".strip()
        joined_str = s.joined_date.strftime('%Y-%m-%d') if s.joined_date else ""
        ws.append([
            idx, f"{s.first_name} {s.last_name}", group_name,
            teacher_name, joined_str, s.payment_rate, amount, phone
        ])

    col_widths = {'A': 8, 'B': 28, 'C': 24, 'D': 22,
                  'E': 16, 'F': 16, 'G': 16, 'H': 20}
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width


# ─────────────────────────────────────────────
# TEACHER DASHBOARD (Jurnal)
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_dashboard(request):
    """
    Tarbiyachi Paneli:
    - Guruh biriktirilgan Tarbiyachi: FAQAT o'ziga biriktirilgan 1 ta guruhni va bolalar davomatini ko'radi.
    - Guruh biriktirilmagan Xodim: Guruh va bolalar bo'limi berkitiladi, faqat Face ID va Xodimlar paneli ochiladi.
    """
    if request.user.role != 'teacher':
        return redirect('home')
    teacher = getattr(request.user, 'teacher_profile', None)
    if not teacher:
        return redirect('staff_my_face_logs')

    # Guruh biriktirilganligini tekshirish - O'qituvchiga tegishli BARCHA guruhlar
    teacher_groups = teacher.groups.all().order_by('name')
    if not teacher_groups.exists():
        messages.info(request, "Sizga hali birorta guruh biriktirilmagan. Siz faqat Face ID va Xodimlar panelidan foydalanishingiz mumkin.")
        return redirect('staff_my_face_logs')

    # URL yoki parametr orqali aniq bitta guruh bo'yicha ixtiyoriy filtrlash
    req_group_id = request.GET.get('group_id')
    selected_group = None
    if req_group_id and str(req_group_id).isdigit():
        selected_group = teacher.groups.filter(id=int(req_group_id)).first()

    # Agar aniq guruh tanlangan bo'lsa - faqat o'sha guruh,
    # aks holda o'qituvchiga biriktirilgan BARCHA guruhlar (fanidan qat'i nazar: matematika, ingliz tili va h.k.) to'liq ko'rsatiladi
    if selected_group:
        groups = teacher.groups.filter(id=selected_group.id).prefetch_related('time_slots__students')
    else:
        groups = teacher_groups.prefetch_related('time_slots__students')

    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    try:
        y, m = map(int, req_month.split('-'))
    except Exception:
        y, m = date.today().year, date.today().month
        req_month = f"{y}-{m:02d}"

    deleted_ids = set(
        DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)
    )

    days_data, num_days = _make_days_data(y, m)

    # Guruhda bo'lgan barcha faol (arxivlanmagan) bolalar
    student_ids = []
    for g in groups:
        for ts in g.time_slots.all():
            for s in ts.students.all():
                if s.id not in deleted_ids and _student_visible_in_month(s, y, m):
                    student_ids.append(s.id)

    attendance_map = {sid: {} for sid in student_ids}
    if student_ids:
        start_d = date(y, m, 1)
        end_d = date(y, m, num_days)
        attendances = Attendance.objects.filter(
            student_id__in=student_ids, date__range=(start_d, end_d))
        for a in attendances:
            attendance_map[a.student_id][a.date.day] = a.status

    for g in groups:
        for ts in g.time_slots.all():
            ts_weekdays = parse_schedule_weekdays(ts.days)
            ts.active_students = [
                s for s in ts.students.all()
                if s.id not in deleted_ids and _student_visible_in_month(s, y, m)
            ]
            for s in ts.active_students:
                matrix = []
                for d in days_data:
                    is_lesson = (d['weekday_idx'] in ts_weekdays)
                    st = attendance_map.get(s.id, {}).get(d['day'], '')
                    is_today = d.get('is_today', False)
                    matrix.append({
                        'day': d['day'],
                        'date_str': d['date_str'],
                        'is_lesson': is_lesson,
                        'status': st,
                        'is_today': is_today,
                        'is_past': d.get('is_past', False),
                        'is_future': d.get('is_future', False),
                        'can_mark': is_lesson and is_today,
                    })
                s.attendance_matrix = matrix

    return render(request, 'teacher_dashboard.html', {
        'teacher': teacher,
        'groups': groups,
        'assigned_group': selected_group,
        'selected_group': selected_group,
        'teacher_groups': teacher_groups,
        'current_month': req_month,
        'days_data': days_data,
    })


# ─────────────────────────────────────────────
# TEACHER ATTENDANCE MARK (AJAX)
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_attendance_mark(request):
    """Tarbiyachi uchun qo'lda bolalar davomati belgilash (AJAX)."""
    if request.user.role != 'teacher' or request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'Bad request'}, status=400)

    student_id = request.POST.get('student_id')
    status = request.POST.get('status', '').strip().lower()
    req_date_raw = request.POST.get('date', '').strip()

    today = timezone.now().date()
    if not req_date_raw:
        req_date = today
    else:
        try:
            req_date = datetime.strptime(req_date_raw, '%Y-%m-%d').date()
        except Exception:
            return JsonResponse({'status': 'error', 'msg': "Noto'g'ri sana formati!"}, status=400)

    # ── Strict Current Day Attendance Validation ──
    if req_date != today:
        return JsonResponse({'status': 'error', 'msg': "Yo'qlama faqat joriy kun (bugun) uchun kiritilishi mumkin!"}, status=400)

    if status in ('green', 'blue'):
        status = 'present'
    elif status in ('red', 'yellow'):
        status = 'absent'

    student = get_object_or_404(Student, id=student_id)
    if not (student.time_slot and student.time_slot.group and
            student.time_slot.group.teacher and
            student.time_slot.group.teacher.user == request.user):
        return JsonResponse({'status': 'error', 'msg': "Ruxsat yo'q! Bu bola sizning guruhingizga tegishli emas."}, status=403)

    if status in ('present', 'absent'):
        try:
            att, _ = Attendance.objects.update_or_create(
                student=student, date=req_date,
                defaults={'status': status},
            )
        except ValidationError as ve:
            err_msg = ve.message_dict.get('date', [str(ve)])[0] if hasattr(ve, 'message_dict') else str(ve)
            return JsonResponse({'status': 'error', 'msg': err_msg}, status=400)

        try:
            from .models import AuditLog
            status_text = "Keldi (+)" if status == 'present' else "Kelmadi (-)"
            AuditLog.objects.create(
                action_type='attendance',
                actor=request.user,
                actor_name=request.user.staff_full_name,
                title=f"Bolalar davomati: {student.first_name} {student.last_name} ({status_text})",
                details=f"Guruh: {student.time_slot.group.name}, Sana: {req_date.isoformat()}"
            )
        except Exception:
            pass
        return JsonResponse({'status': 'success', 'attendance': att.status})
    else:
        Attendance.objects.filter(student=student, date=req_date).delete()
        try:
            from .models import AuditLog
            AuditLog.objects.create(
                action_type='attendance',
                actor=request.user,
                actor_name=request.user.staff_full_name,
                title=f"Bolalar davomati bekor qilindi: {student.first_name} {student.last_name}",
                details=f"Guruh: {student.time_slot.group.name}, Sana: {req_date.isoformat()}"
            )
        except Exception:
            pass
        return JsonResponse({'status': 'success', 'attendance': ''})


# ─────────────────────────────────────────────
# TEACHER STUDENT CREATE
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_student_create(request, timeslot_id):
    if request.user.role != 'teacher':
        return redirect('home')
    timeslot = get_object_or_404(TimeSlot, id=timeslot_id)
    if not (timeslot.group and timeslot.group.teacher and
            timeslot.group.teacher.user == request.user):
        return redirect('teacher_dashboard')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        age = request.POST.get('age', '').strip()
        phone_1 = request.POST.get('phone_1', '').strip()
        phone_2 = request.POST.get('phone_2', '').strip()
        photo = request.FILES.get('photo')

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(
                request, "Ism va familiyada faqat lotin alifbosidan foydalaning "
                "(Kirill harflari taqiqlangan)!")
            return render(request, 'teacher_student_create.html', {
                'timeslot': timeslot,
                'first_name_val': first_name,
                'last_name_val': last_name,
                'age_val': age,
                'phone_1_val': phone_1,
                'phone_2_val': phone_2,
            })

        if first_name and last_name and phone_1:
            is_local_val = (request.POST.get('is_local') in ('1', 'on', 'true', True))
            student = Student(
                first_name=first_name,
                last_name=last_name,
                phone_1=phone_1,
                phone_2=phone_2,
                time_slot=timeslot,
                is_local=is_local_val,
            )
            if age.isdigit():
                student.age = int(age)
            if photo:
                opt_photo, err = optimize_student_photo(photo)
                student.photo = opt_photo if opt_photo else photo
            student.save()
            messages.success(
                request, f"O'quvchi '{first_name} {last_name}' muvaffaqiyatli qo'shildi.")
            return redirect('teacher_dashboard')
    return render(request, 'teacher_student_create.html', {'timeslot': timeslot})


# ─────────────────────────────────────────────
# TEACHER STUDENT DELETE
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_student_delete(request, id):
    """O'qituvchi o'z guruhidagi o'quvchini arxivga yuborishi."""
    if request.user.role != 'teacher':
        return redirect('home')
    student = get_object_or_404(Student, id=id)

    if not (student.time_slot and student.time_slot.group and
            student.time_slot.group.teacher and
            student.time_slot.group.teacher.user == request.user):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'msg': "Bu o'quvchi sizning guruhingizga biriktirilmagan."}, status=403)
        messages.error(request, "Bu o'quvchi sizning guruhingizga biriktirilmagan.")
        return redirect('teacher_dashboard')

    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            reason = "Guruhdan chiqarildi"
        elif contains_cyrillic(reason):
            reason = "Guruhdan chiqarildi"

        group_name = (student.time_slot.group.name
                      if (student.time_slot and student.time_slot.group) else "Noma'lum")
        group_id = student.time_slot.group.id if (student.time_slot and student.time_slot.group) else None
        st_full_name = f"{student.first_name} {student.last_name}".strip()

        DeletedStudent.objects.create(
            student_name=st_full_name,
            phones=f"{student.phone_1}, {student.phone_2 or ''}".rstrip(', '),
            group_name=group_name,
            reason=reason,
            first_name=student.first_name,
            last_name=student.last_name,
            age=student.age,
            phone_1=student.phone_1,
            phone_2=student.phone_2,
            time_slot_id=student.time_slot.id if student.time_slot else None,
            student_id=student.id,
        )
        student.is_active = False
        student.left_date = date.today()
        student.time_slot = None
        student.save()

        # AJAX so'rovi bo'lsa JSON qaytarish
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or 'application/json' in request.headers.get('Accept', ''):
            return JsonResponse({
                'status': 'success',
                'msg': f"O'quvchi '{st_full_name}' arxivga muvaffaqiyatli o'tkazildi.",
                'student_id': id
            })

        messages.success(
            request, f"O'quvchi '{st_full_name}' arxivga muvaffaqiyatli o'tkazildi.")
        if group_id:
            return redirect(f"/teacher/dashboard/?group_id={group_id}")
        return redirect('teacher_dashboard')
    return redirect('teacher_dashboard')


# ─────────────────────────────────────────────
# TEACHER STUDENT EDIT
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_student_edit(request, id):
    """O'qituvchi tomonidan o'quvchi ma'lumotlarini tahrirlash (faqat admin ruxsat bersa)."""
    if request.user.role != 'teacher':
        return redirect('home')
    student = get_object_or_404(Student, id=id)

    if not (student.time_slot and student.time_slot.group and
            student.time_slot.group.teacher and
            student.time_slot.group.teacher.user == request.user):
        messages.error(request, "Bu o'quvchi sizning guruhingizga tegishli emas.")
        return redirect('teacher_dashboard')

    teacher = request.user.teacher_profile
    if not teacher.can_edit_students:
        messages.error(
            request, "Sizda o'quvchini tahrirlash ruxsati yo'q. Admin bilan bog'laning.")
        return redirect('teacher_dashboard')

    next_url = request.GET.get('next') or request.POST.get('next', '')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        age_raw = request.POST.get('age', '').strip()
        phone_1 = request.POST.get('phone_1', '').strip()
        phone_2 = request.POST.get('phone_2', '').strip()

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning!")
        else:
            student.first_name = first_name or student.first_name
            student.last_name = last_name or student.last_name
            student.age = int(age_raw) if age_raw.isdigit() else student.age
            student.phone_1 = phone_1 or student.phone_1
            student.phone_2 = phone_2
            if 'is_local' in request.POST:
                student.is_local = (
                    request.POST.get('is_local') in ('1', 'on', 'true', True))
            joined_date_raw = request.POST.get('joined_date', '').strip()
            if joined_date_raw:
                try:
                    parsed_joined = datetime.strptime(joined_date_raw, '%Y-%m-%d').date()
                    if parsed_joined > date.today():
                        messages.error(
                            request,
                            "Kelgan sana kelajakdagi sana bo'lishi mumkin emas! "
                            "Bugungi yoki o'tgan sanani kiriting.")
                        return render(request, 'student_edit.html', {
                            'student': student,
                            'next': next_url,
                            'role': 'teacher',
                        })
                    student.joined_date = parsed_joined
                except Exception:
                    pass
            student.save()
            messages.success(request, "O'quvchi ma'lumotlari yangilandi.")
            if next_url:
                return redirect(next_url)
            return redirect('teacher_dashboard')

    return render(request, 'student_edit.html', {
        'student': student,
        'next': next_url,
        'role': 'teacher',
    })


# ─────────────────────────────────────────────
# TEACHER EXCEL EXPORT
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_export_excel(request):
    """O'qituvchining o'z kabinetidan o'ziga biriktirilgan o'quvchilarni Excel eksport."""
    teacher = getattr(request.user, 'teacher_profile', None)
    if not teacher:
        teacher = Teacher.objects.filter(user=request.user).first()
    if not teacher:
        return redirect('home')

    curr_month = date.today().strftime('%Y-%m')
    payments = MonthlyPayment.objects.filter(month=curr_month)
    payment_map = {p.student_id: p.amount_paid for p in payments}

    req_group_id = request.GET.get('group_id')
    deleted_ids = DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)
    students = (
        Student.objects.select_related('time_slot__group__teacher')
        .filter(
            time_slot__group__teacher=teacher,
            is_active=True,
            left_date__isnull=True,
            time_slot__isnull=False,
            time_slot__group__isnull=False,
        )
        .exclude(id__in=deleted_ids)
    )
    if req_group_id and str(req_group_id).isdigit():
        students = students.filter(time_slot__group_id=int(req_group_id))
    students = students.order_by('time_slot__group__name', 'first_name', 'last_name')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{teacher.first_name}_{teacher.last_name}"[:28]
    _populate_excel_sheet(ws, students, payment_map)

    response = HttpResponse(
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = (
        f'attachment; filename="Oqituvchi_'
        f'{teacher.first_name}_{teacher.last_name}_{curr_month}.xlsx"')
    wb.save(response)
    return response


# ─────────────────────────────────────────────
# TEACHER CHAT
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_chat(request):
    if request.user.role != 'teacher':
        return redirect('home')

    admin_user = CustomUser.objects.filter(role='admin').first()
    if not admin_user:
        from django.http import HttpResponse as HR
        return HR("Admin topilmadi", status=404)

    Message.objects.filter(
        sender=admin_user, receiver=request.user, is_read=False
    ).update(is_read=True)

    messages_qs = Message.objects.filter(
        (Q(sender=request.user) & Q(receiver=admin_user)) |
        (Q(sender=admin_user) & Q(receiver=request.user))
    ).order_by('timestamp')

    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        if content:
            Message.objects.create(
                sender=request.user, receiver=admin_user, content=content)
            return redirect('teacher_chat')

    return render(request, 'teacher_chat.html', {
        'chat_user': admin_user,
        'chat_messages': messages_qs,
    })
