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
    O'quvchi faol bo'lsa (is_active=True), joined_date kelajakda yoki o'tmishda
    bo'lishidan qat'i nazar doimo jurnalda ko'rinadi.
    Arxivlangan/o'chirilgan (is_active=False) o'quvchilar guruh jurnalidan chiqariladi.
    """
    return bool(getattr(st, 'is_active', True))


def _build_attendance_matrix(students, days_data, attendance_map):
    """Har bir o'quvchi uchun davomat matritsasini tuzadi."""
    for s in students:
        matrix = []
        for d in days_data:
            st = attendance_map.get(s.id, {}).get(d['day'], '')
            matrix.append({
                'day': d['day'],
                'date_str': d['date_str'],
                'is_lesson': d.get('is_lesson', False),
                'status': st,
            })
        s.attendance_matrix = matrix


def _make_days_data(year, month):
    """Berilgan oy uchun kunlar ro'yxatini tayyorlaydi."""
    num_days = calendar.monthrange(year, month)[1]
    weekdays_uz = ['Du', 'Se', 'Chor', 'Pay', 'Ju', 'Sha', 'Yak']
    today_date = date.today()
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
    if request.user.role != 'teacher':
        return redirect('home')
    teacher = request.user.teacher_profile
    groups = teacher.groups.prefetch_related('time_slots__students')

    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    try:
        y, m = map(int, req_month.split('-'))
    except Exception:
        y, m = date.today().year, date.today().month
        req_month = f"{y}-{m:02d}"

    days_data, num_days = _make_days_data(y, m)

    # Guruhda bo'lgan barcha o'quvchilar (is_active, joined_date'ga qaramay)
    student_ids = []
    for g in groups:
        for ts in g.time_slots.all():
            for s in ts.students.all():
                if _student_visible_in_month(s, y, m):
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
                if _student_visible_in_month(s, y, m)
            ]
            for s in ts.active_students:
                matrix = []
                for d in days_data:
                    is_lesson = (d['weekday_idx'] in ts_weekdays)
                    st = attendance_map.get(s.id, {}).get(d['day'], '')
                    matrix.append({
                        'day': d['day'],
                        'date_str': d['date_str'],
                        'is_lesson': is_lesson,
                        'status': st,
                    })
                s.attendance_matrix = matrix

    return render(request, 'teacher_dashboard.html', {
        'teacher': teacher,
        'groups': groups,
        'current_month': req_month,
        'days_data': days_data,
    })


# ─────────────────────────────────────────────
# TEACHER ATTENDANCE MARK (AJAX)
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_attendance_mark(request):
    """O'qituvchi uchun davomat belgilash (AJAX). Toggle: present -> absent -> none"""
    if request.user.role != 'teacher' or request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'Bad request'}, status=400)

    student_id = request.POST.get('student_id')
    status = request.POST.get('status', '').strip().lower()
    req_date = request.POST.get('date', date.today().isoformat())

    if status in ('green', 'blue'):
        status = 'present'
    elif status in ('red', 'yellow'):
        status = 'absent'

    student = get_object_or_404(Student, id=student_id)
    if not (student.time_slot and student.time_slot.group and
            student.time_slot.group.teacher and
            student.time_slot.group.teacher.user == request.user):
        return JsonResponse({'status': 'error', 'msg': "Ruxsat yo'q"}, status=403)

    if status in ('present', 'absent'):
        att, _ = Attendance.objects.update_or_create(
            student=student, date=req_date,
            defaults={'status': status},
        )
        return JsonResponse({'status': 'success', 'attendance': att.status})
    else:
        Attendance.objects.filter(student=student, date=req_date).delete()
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
        messages.error(request, "Bu o'quvchi sizning guruhingizga biriktirilmagan.")
        return redirect('teacher_dashboard')

    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            messages.error(request, "O'quvchini o'chirish uchun sabab kiritish majburiy!")
            return redirect('teacher_dashboard')

        if contains_cyrillic(reason):
            messages.error(
                request,
                "O'chirish sababida faqat lotin alifbosidagi harflardan foydalaning!")
            return redirect('teacher_dashboard')

        group_name = (student.time_slot.group.name
                      if (student.time_slot and student.time_slot.group) else "Noma'lum")
        st_full_name = f"{student.first_name} {student.last_name}"

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
        messages.success(
            request, f"O'quvchi '{st_full_name}' arxivga muvaffaqiyatli o'tkazildi.")
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

    students = Student.objects.select_related('time_slot__group__teacher').filter(
        time_slot__group__teacher=teacher
    ).order_by('time_slot__group__name', 'first_name', 'last_name')

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
