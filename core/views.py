"""
HUMO O'quv markazi - Core Views
Admin: custom Google OAuth2 + OTP two-step auth, standard password fallback.
Teacher: generated login/password.
Parent: read-only attendance view.
"""
import os
import random
import string
import requests
import re

from datetime import date, datetime
try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    relativedelta = None

from django.conf import settings
from django.contrib.auth import login, logout, authenticate, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .models import (
    Attendance, CustomUser, DeletedStudent, Group, Student, Teacher,
    TemporaryStudent, TimeSlot, MonthlyPayment
)
from .forms import TemporaryStudentForm
from .services import optimize_student_photo
from django.db.models import Q
import calendar
import openpyxl
from django.http import HttpResponse

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _make_otp() -> str:
    return str(random.randint(100000, 999999))


def _send_otp(email: str, otp: str, subject: str = "HUMO O'quv markazi – OTP Kod"):
    body = (
        f"Sizning bir martalik tasdiqlash kodingiz:\n\n"
        f"  {otp}\n\n"
        f"Ushbu kod faqat bir marta va qisqa vaqt ichida amal qiladi.\n"
        f"Agar siz so'rov qilmagan bo'lsangiz, ushbu xabarni e'tiborsiz qoldiring."
    )
    try:
        send_mail(subject, body, settings.EMAIL_HOST_USER, [email], fail_silently=False)
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────
# HOME
# ─────────────────────────────────────────────

def home(request):
    return render(request, 'home.html')


def user_logout(request):
    logout(request)
    return redirect('home')


# ─────────────────────────────────────────────
# ADMIN AUTH (Static Credentials)
# ─────────────────────────────────────────────
from functools import wraps

def admin_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.session.get('admin_authenticated'):
            return redirect('admin_login')
        return view_func(request, *args, **kwargs)
    return _wrapped_view

def admin_login(request):
    if request.session.get('admin_authenticated'):
        return redirect('admin_dashboard')

    error = None
    if request.method == 'POST':
        username = request.POST.get('login', '').strip()
        password = request.POST.get('password', '').strip()
        
        if username == 'teacher' and password == 'teach002':
            request.session['admin_authenticated'] = True
            return redirect('admin_dashboard')
        else:
            error = "Login yoki parol noto'g'ri."

    return render(request, 'admin_login.html', {'error': error})

def admin_logout(request):
    request.session.pop('admin_authenticated', None)
    return redirect('home')

# ─────────────────────────────────────────────
# ADMIN PANEL VIEWS
# ─────────────────────────────────────────────

def _admin_only(request):
    return request.session.get('admin_authenticated')


from django.contrib import messages
from .validators import contains_cyrillic

@admin_required
def admin_dashboard(request):
    if not _admin_only(request):
        return redirect('home')
    
    query = request.GET.get('q', '').strip()
    search_results = []
    
    if query:
        words = query.split()
        q_filter = Q()
        for w in words:
            q_filter |= Q(first_name__icontains=w) | Q(last_name__icontains=w)
            
        search_results = Student.objects.filter(q_filter, is_active=True).select_related(
            'time_slot__group__teacher'
        ).prefetch_related('monthly_payments').order_by('first_name', 'last_name')

    groups = Group.objects.select_related('teacher').prefetch_related('time_slots__students')
    teachers = Teacher.objects.all().prefetch_related('groups')
    
    return render(request, 'admin_dashboard.html', {
        'groups': groups, 
        'teachers': teachers,
        'search_query': query,
        'search_results': search_results,
    })


@admin_required
def admin_group_create(request):
    if not _admin_only(request):
        return redirect('home')
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        teacher_id = request.POST.get('teacher_id')
        
        if contains_cyrillic(name):
            messages.error(request, "Guruh nomida faqat lotin alifbosidagi harflardan foydalaning (Kirill harflari taqiqlangan)!")
            teachers = Teacher.objects.all()
            return render(request, 'admin_group_create.html', {'teachers': teachers, 'name_val': name})
            
        teacher = get_object_or_404(Teacher, id=teacher_id) if teacher_id else None
        subject = request.POST.get('subject', '').strip() or name
        Group.objects.create(name=name, subject=subject, teacher=teacher)
        messages.success(request, f"'{name}' guruhi muvaffaqiyatli yaratildi.")
        return redirect('admin_dashboard')
    teachers = Teacher.objects.all()
    return render(request, 'admin_group_create.html', {'teachers': teachers})


@admin_required
def admin_group_delete(request, id):
    if not _admin_only(request):
        return redirect('home')
    group = get_object_or_404(Group, id=id)
    if request.method == 'POST':
        # Guruhdagi o'quvchilarni soft delete orqali arxivlash
        for ts in group.time_slots.all():
            for student in ts.students.filter(is_active=True):
                # O'quvchi ma'lumotlari arxivga saqlanadi
                DeletedStudent.objects.create(
                    student_name=f"{student.first_name} {student.last_name}",
                    phones=f"{student.phone_1}, {student.phone_2 or ''}".rstrip(', '),
                    group_name=group.name,
                    reason=f"Guruh o'chirilishi sababli arxivlandi ({group.name})",
                    first_name=student.first_name,
                    last_name=student.last_name,
                    age=student.age,
                    phone_1=student.phone_1,
                    phone_2=student.phone_2,
                    time_slot_id=None,
                    student_id=student.id,
                )
                # Soft delete — o'tgan oylar ma'lumotlari saqlanadi
                student.is_active = False
                student.left_date = date.today()
                student.save()
        group.delete()
        messages.success(request, f"'{group.name}' guruhi o'chirildi va o'quvchilar arxivlandi.")
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')


@admin_required
def admin_teacher_delete(request, id):
    if not _admin_only(request):
        return redirect('home')
    teacher = get_object_or_404(Teacher, id=id)
    if request.method == 'POST':
        user = teacher.user
        teacher.delete()
        if user:
            user.delete()
        messages.success(request, f"O'qituvchi o'chirildi.")
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')


@admin_required
def admin_timeslot_delete(request, id):
    if not _admin_only(request):
        return redirect('home')
    timeslot = get_object_or_404(TimeSlot, id=id)
    group_id = timeslot.group.id
    if request.method == 'POST':
        timeslot.delete()
        messages.success(request, "Dars vaqti o'chirildi.")
    return redirect('admin_group_detail', id=group_id)


@admin_required
def admin_group_detail(request, id):
    if not _admin_only(request):
        return redirect('home')
    group = get_object_or_404(Group, id=id)
    
    if request.method == 'POST':
        days = request.POST.get('days', '').strip()
        start_time = request.POST.get('start_time', '').strip()
        end_time = request.POST.get('end_time', '').strip()
        
        if contains_cyrillic(days):
            messages.error(request, "Dars kunlarida faqat lotin alifbosidagi harflardan foydalaning!")
            return redirect('admin_group_detail', id=group.id)
            
        if days and start_time and end_time:
            TimeSlot.objects.create(
                group=group, 
                days=days, 
                start_time=start_time, 
                end_time=end_time
            )
            messages.success(request, "Yangi dars vaqti muvaffaqiyatli qo'shildi.")
        return redirect('admin_group_detail', id=group.id)

    # Jurnal oynasi (Oy bo'yicha)
    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    try:
        y, m = map(int, req_month.split('-'))
    except:
        y, m = date.today().year, date.today().month
        req_month = f"{y}-{m:02d}"

    num_days = calendar.monthrange(y, m)[1]
    weekdays_uz = ['Du', 'Se', 'Chor', 'Pay', 'Ju', 'Sha', 'Yak']
    today_date = date.today()

    days_data = []
    for d in range(1, num_days + 1):
        cur_d = date(y, m, d)
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
    
    timeslots = group.time_slots.prefetch_related('students')

    month_end = date(y, m, num_days)

    def _student_in_month(st):
        if st.joined_date and st.joined_date > month_end:
            return False
        if st.left_date:
            return st.left_date > month_end
        return st.is_active

    student_ids = []
    for ts in timeslots:
        for s in ts.students.all():
            if _student_in_month(s):
                student_ids.append(s.id)

    attendance_map = {sid: {} for sid in student_ids}
    if student_ids:
        start_d = date(y, m, 1)
        end_d = date(y, m, num_days)
        attendances = Attendance.objects.filter(student_id__in=student_ids, date__range=(start_d, end_d))
        for a in attendances:
            attendance_map[a.student_id][a.date.day] = a.status

    # Annotate students with smart schedule attendance matrix
    from .schedule_helper import parse_schedule_weekdays
    for ts in timeslots:
        ts_weekdays = parse_schedule_weekdays(ts.days)
        ts.active_students = [s for s in ts.students.all() if _student_in_month(s)]
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

    return render(request, 'admin_group_detail.html', {
        'group': group,
        'timeslots': timeslots,
        'current_month': req_month,
        'days_data': days_data,
    })


@admin_required
def admin_teacher_create(request):
    if not _admin_only(request):
        return redirect('home')
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning (Kirill harflari taqiqlangan)!")
            return render(request, 'admin_teacher_create.html', {
                'first_name_val': first_name,
                'last_name_val': last_name,
                'phone_val': phone,
            })

        login_str = f"t_{first_name.lower()[:3]}_{random.randint(100, 999)}"
        pwd_str = ''.join(random.choices(string.ascii_letters + string.digits, k=8))

        user = CustomUser.objects.create_user(username=login_str, password=pwd_str, role='teacher')
        Teacher.objects.create(
            user=user,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            generated_login=login_str,
            generated_password=pwd_str,
        )
        messages.success(request, f"O'qituvchi '{first_name} {last_name}' muvaffaqiyatli yaratildi.")
        return redirect('admin_dashboard')
    return render(request, 'admin_teacher_create.html')


@admin_required
def admin_deleted_students(request):
    if not _admin_only(request):
        return redirect('home')
    deleted = DeletedStudent.objects.all().order_by('-deleted_at')
    timeslots = TimeSlot.objects.select_related('group').all()
    return render(request, 'admin_deleted_students.html', {'deleted': deleted, 'timeslots': timeslots})

@admin_required
def admin_deleted_student_restore(request, id):
    if not _admin_only(request):
        return redirect('home')
    ds = get_object_or_404(DeletedStudent, id=id)
    if request.method == 'POST':
        timeslot_id = request.POST.get('timeslot_id')
        if timeslot_id:
            ts = get_object_or_404(TimeSlot, id=timeslot_id)
            student = None
            if ds.student_id:
                student = Student.objects.filter(id=ds.student_id).first()
            if student:
                student.is_active = True
                student.left_date = None
                student.time_slot = ts
                student.save()
            else:
                Student.objects.create(
                    first_name=ds.first_name or (ds.student_name.split()[0] if ds.student_name else "Ism"),
                    last_name=ds.last_name or (ds.student_name.split()[1] if len(ds.student_name.split()) > 1 else ""),
                    age=ds.age or 0,
                    phone_1=ds.phone_1 or (ds.phones.split(',')[0].strip() if ds.phones else ""),
                    phone_2=ds.phone_2 or (ds.phones.split(',')[1].strip() if ',' in ds.phones else ""),
                    time_slot=ts,
                    is_active=True
                )
            ds.delete()
            messages.success(request, "O'quvchi guruhga qayta tiklandi.")
    return redirect('admin_deleted_students')

@admin_required
def admin_deleted_student_delete_permanent(request, id):
    if not _admin_only(request):
        return redirect('home')
    if request.method == 'POST':
        ds = get_object_or_404(DeletedStudent, id=id)
        if ds.student_id:
            Student.objects.filter(id=ds.student_id).delete()
        ds.delete()
        messages.success(request, "O'quvchi arxivdan butunlay o'chirildi.")
    return redirect('admin_deleted_students')


@admin_required
def admin_student_create(request, timeslot_id):
    if not _admin_only(request):
        return redirect('home')
    timeslot = get_object_or_404(TimeSlot, id=timeslot_id)
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        age = request.POST.get('age', '').strip()
        phone_1 = request.POST.get('phone_1', '').strip()
        phone_2 = request.POST.get('phone_2', '').strip()
        photo = request.FILES.get('photo')

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning (Kirill harflari taqiqlangan)!")
            return render(request, 'admin_student_create.html', {
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
                is_local=is_local_val
            )
            if age.isdigit():
                student.age = int(age)
            if photo:
                opt_photo, err = optimize_student_photo(photo)
                student.photo = opt_photo if opt_photo else photo
            student.save()

            messages.success(request, f"O'quvchi '{first_name} {last_name}' muvaffaqiyatli qo'shildi.")
            return redirect('admin_group_detail', id=timeslot.group.id)
    return render(request, 'admin_student_create.html', {'timeslot': timeslot})


@admin_required
def admin_student_delete(request, id):
    if not _admin_only(request):
        return redirect('home')
    student = get_object_or_404(Student, id=id)
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if contains_cyrillic(reason):
            messages.error(request, "O'chirish sababida faqat lotin harflaridan foydalaning!")
            reason = "Admin tomonidan o'chirildi"

        group_id = student.time_slot.group.id if student.time_slot else None

        # Soft delete: o'quvchi o'chirilmaydi, faqat left_date va is_active belgilanadi
        # Bu orqali o'tgan oylarning davomati va to'lov tarixi saqlanib qoladi
        student.is_active = False
        student.left_date = date.today()
        student.save()

        DeletedStudent.objects.create(
            student_name=f"{student.first_name} {student.last_name}",
            phones=f"{student.phone_1}, {student.phone_2 or ''}".rstrip(', '),
            group_name=student.time_slot.group.name if student.time_slot else "Noma'lum",
            reason=reason or "Admin tomonidan o'chirildi",
            first_name=student.first_name,
            last_name=student.last_name,
            age=student.age,
            phone_1=student.phone_1,
            phone_2=student.phone_2,
            time_slot_id=student.time_slot.id if student.time_slot else None,
            student_id=student.id,
        )
        messages.success(request, f"O'quvchi arxivga o'tkazildi.")
        next_url = request.POST.get('next') or request.GET.get('next')
        if next_url:
            return redirect(next_url)
        if group_id:
            return redirect('admin_group_detail', id=group_id)
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')

# ─────────────────────────────────────────────
# TO'G'RIDAN-TO'G'RI EXCEL EKSPORT TIZIMI (DIRECT EXPORT)
# ─────────────────────────────────────────────

def _populate_excel_sheet(ws, students, payment_map):
    """
    Excel varag'iga (sheet) standart ustunlar va o'quvchilar ro'yxatini yozish.
    Ustunlar tartibi:
    № | F.I.O | Fan (Guruh) | O'qituvchi | Kelgan sana | To'lov tari | To'lov miq | Telefon raqami
    Sanoq/tartib raqami chap tarafda bevosita o'quvchilardan boshlanadi (1, 2, 3...)
    """
    headers = [
        "№", "F.I.O", "Fan (Guruh)", "O'qituvchi", "Kelgan sana", 
        "To'lov tari", "To'lov miq", "Telefon raqami"
    ]
    ws.append(headers)

    # Sarlavha stillari (premium indigo fon, oq matn)
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF", size=11)
        cell.fill = openpyxl.styles.PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid")
        cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")

    for idx, s in enumerate(students, 1):
        group_name = s.time_slot.group.name if (s.time_slot and s.time_slot.group) else "-"
        teacher_name = str(s.time_slot.group.teacher) if (s.time_slot and s.time_slot.group and s.time_slot.group.teacher) else "-"
        amount = payment_map.get(s.id, "")
        phone = f"{s.phone_1} {s.phone_2 or ''}".strip()
        joined_str = s.joined_date.strftime('%Y-%m-%d') if s.joined_date else ""

        ws.append([
            idx,
            f"{s.first_name} {s.last_name}",
            group_name,
            teacher_name,
            joined_str,
            s.payment_rate,
            amount,
            phone
        ])

    # Ustun kengliklari
    col_widths = {'A': 8, 'B': 28, 'C': 24, 'D': 22, 'E': 16, 'F': 16, 'G': 16, 'H': 20}
    for col_letter, width in col_widths.items():
        ws.column_dimensions[col_letter].width = width

@admin_required
def admin_export_excel(request):
    """
    Admin: Barcha o'qituvchilar va ularga biriktirilgan guruhlar/o'quvchilar ro'yxatini
    to'g'ridan-to'g'ri Excel formatida kompyuter yoki telefonga yuklab berish.
    """
    if not _admin_only(request):
        return redirect('home')

    curr_month = date.today().strftime('%Y-%m')
    payments = MonthlyPayment.objects.filter(month=curr_month)
    payment_map = {p.student_id: p.amount_paid for p in payments}

    wb = openpyxl.Workbook()
    
    # 1. Barcha o'quvchilar varag'i (O'qituvchi va guruh kesimida tartiblangan)
    ws_all = wb.active
    ws_all.title = "Barcha O'quvchilar"
    all_students = Student.objects.select_related('time_slot__group__teacher').filter(is_active=True).order_by(
        'time_slot__group__teacher__first_name',
        'time_slot__group__teacher__last_name',
        'time_slot__group__name',
        'first_name',
        'last_name'
    )
    _populate_excel_sheet(ws_all, all_students, payment_map)

    # 2. Har bir o'qituvchi uchun alohida varaq (sheet)
    teachers = Teacher.objects.all().order_by('first_name', 'last_name')
    for t in teachers:
        t_students = [s for s in all_students if s.time_slot and s.time_slot.group and s.time_slot.group.teacher_id == t.id]
        if t_students:
            sheet_title = f"{t.first_name} {t.last_name}"[:30]
            ws_t = wb.create_sheet(title=sheet_title)
            _populate_excel_sheet(ws_t, t_students, payment_map)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="HUMO_Oquvchilar_{curr_month}.xlsx"'
    wb.save(response)
    return response

@admin_required
def admin_group_export_excel(request, group_id):
    """
    Guruh sahifasidan ma'lum bir guruh va o'qituvchi o'quvchilarini
    to'g'ridan-to'g'ri Excel fayl qilib yuklab berish.
    """
    if not _admin_only(request):
        return redirect('home')

    group = get_object_or_404(Group, id=group_id)
    curr_month = date.today().strftime('%Y-%m')
    payments = MonthlyPayment.objects.filter(month=curr_month)
    payment_map = {p.student_id: p.amount_paid for p in payments}

    students = Student.objects.select_related('time_slot__group__teacher').filter(
        time_slot__group=group, is_active=True
    ).order_by('first_name', 'last_name')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{group.name[:28]}"
    _populate_excel_sheet(ws, students, payment_map)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="Guruh_{group.name}_{curr_month}.xlsx"'
    wb.save(response)
    return response

@login_required(login_url='/teacher/login/')
def teacher_export_excel(request):
    """
    O'qituvchining o'z kabinetidan o'ziga biriktirilgan barcha o'quvchilarni
    guruhlar kesimida to'g'ridan-to'g'ri Excel fayl qilib yuklab berish.
    """
    teacher = getattr(request.user, 'teacher_profile', None)
    if not teacher:
        teacher = Teacher.objects.filter(user=request.user).first()
    if not teacher:
        return redirect('home')

    curr_month = date.today().strftime('%Y-%m')
    payments = MonthlyPayment.objects.filter(month=curr_month)
    payment_map = {p.student_id: p.amount_paid for p in payments}

    students = Student.objects.select_related('time_slot__group__teacher').filter(
        time_slot__group__teacher=teacher, is_active=True
    ).order_by('time_slot__group__name', 'first_name', 'last_name')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{teacher.first_name}_{teacher.last_name}"[:28]
    _populate_excel_sheet(ws, students, payment_map)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="Oqituvchi_{teacher.first_name}_{teacher.last_name}_{curr_month}.xlsx"'
    wb.save(response)
    return response


# ─────────────────────────────────────────────
# ADMIN: Temporary Students (Vaqtinchalik)
# ─────────────────────────────────────────────

@admin_required
def admin_temp_students(request):
    """Vaqtinchalik o'quvchilar ro'yxati va qo'shish formasi."""
    if not _admin_only(request):
        return redirect('home')

    if request.method == 'POST':
        form = TemporaryStudentForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('admin_temp_students')
    else:
        form = TemporaryStudentForm()

    temp_students = TemporaryStudent.objects.all()
    timeslots = TimeSlot.objects.select_related('group', 'group__teacher').all()

    return render(request, 'admin_temp_students.html', {
        'form': form,
        'temp_students': temp_students,
        'timeslots': timeslots,
    })


@admin_required
def admin_temp_student_assign(request, id):
    """Vaqtinchalik o'quvchini aniq guruh/soatga yo'naltirish."""
    if not _admin_only(request):
        return redirect('home')

    temp = get_object_or_404(TemporaryStudent, id=id)

    if request.method == 'POST':
        timeslot_id = request.POST.get('timeslot_id')
        age = request.POST.get('age', '0').strip()

        if timeslot_id:
            ts = get_object_or_404(TimeSlot, id=timeslot_id)
            Student.objects.create(
                first_name=temp.first_name,
                last_name=temp.last_name,
                age=int(age) if age.isdigit() else 0,
                phone_1=temp.phone_1,
                phone_2=temp.phone_2 or '',
                time_slot=ts,
            )
            temp.delete()

    return redirect('admin_temp_students')


@admin_required
def admin_temp_student_delete(request, id):
    """Vaqtinchalik o'quvchini o'chirish."""
    if not _admin_only(request):
        return redirect('home')
    if request.method == 'POST':
        temp = get_object_or_404(TemporaryStudent, id=id)
        temp.delete()
    return redirect('admin_temp_students')


@admin_required
def admin_attendance_mark(request):
    """Admin uchun davomat belgilash (AJAX endpoint). Toggle: present -> absent -> none"""
    if not _admin_only(request):
        return JsonResponse({'status': 'error', 'msg': 'Ruxsat yo\'q'}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'Noto\'g\'ri so\'rov'}, status=400)

    student_id = request.POST.get('student_id')
    status_val = request.POST.get('status', '').strip().lower()
    req_date = request.POST.get('date', date.today().isoformat())

    # Map backwards compatible status values
    if status_val in ('green', 'blue'):
        status_val = 'present'
    elif status_val in ('red', 'yellow'):
        status_val = 'absent'

    student = get_object_or_404(Student, id=student_id)
    
    if status_val in ('present', 'absent'):
        att, _ = Attendance.objects.update_or_create(
            student=student, date=req_date,
            defaults={'status': status_val},
        )
        return JsonResponse({'status': 'success', 'attendance': status_val})
    else:
        # Bo'shatish yoki o'chirish holati
        Attendance.objects.filter(student=student, date=req_date).delete()
        return JsonResponse({'status': 'success', 'attendance': ''})

# ─────────────────────────────────────────────
# ADMIN PAYMENTS (To'lovlar boshqaruvi)
# ─────────────────────────────────────────────

def _clean_amount(val) -> int:
    """To'lov qiymatidan sof raqamni ajratib oladi. Matn ('mahalla', 'm', va h.k.) bo'lsa 0 qaytaradi."""
    if not val:
        return 0
    s = str(val).replace(' ', '').replace("'", '').replace(',', '').strip()
    return int(s) if s.isdigit() else 0


def _build_payments_data(students_qs, req_month, status_filter='all', is_group_specific=False):
    """
    O'quvchilar va ularning to'lovlari bo'yicha ma'lumotlar ro'yxatini shakllantiradi.
    status_filter: 'all', 'paid', 'unpaid', 'overdue', 'local'

    Mantiq:
      1. ARXIV: Tanlangan oy (req_month) da guruhda bo'lgan yoki shu oy uchun to'lov yozilgan
         barcha o'quvchilar ro'yxatga olinadi (left_date va joined_date hamda mavjud to'lovlar asosida).
      2. OYLIK TUSHUM (total_paid): Tanlangan oy uchun yig'ilgan BARCHA to'lovlar summasi.
      3. BUGUNGI TUSHUM (daily_collected): Tanlangan oy uchun aynan BUGUN (today) to'langan
         to'lovlar summasi (boshqa oylar yoki boshqa guruhlar bu summaga aralashmaydi).
    """
    today = date.today()
    try:
        req_year, req_mon = map(int, req_month.split('-'))
    except Exception:
        req_year, req_mon = today.year, today.month
        req_month = f"{req_year:04d}-{req_mon:02d}"

    max_day_in_month = calendar.monthrange(req_year, req_mon)[1]
    month_start = date(req_year, req_mon, 1)
    month_end = date(req_year, req_mon, max_day_in_month)

    # 1. Tanlangan oyda to'lov yozilgan o'quvchilar IDlari:
    paid_student_ids = set(
        MonthlyPayment.objects.filter(student__in=students_qs, month=req_month)
        .values_list('student_id', flat=True)
    )

    # 2. Tanlangan oyda mavjud bo'lgan o'quvchilarni olish (oylik arxiv mantig'i):
    # - Shu oy uchun to'lovi borlar
    # - YOKI shu oy davomida guruhda o'qiganlar (joined_date <= month_end va (left_date yo'q yoki left_date >= month_start))
    students_in_month = students_qs.filter(
        Q(id__in=paid_student_ids) |
        (
            (Q(joined_date__lte=month_end) | Q(joined_date__isnull=True)) &
            (
                Q(left_date__isnull=True, is_active=True) |
                Q(left_date__gte=month_start)
            )
        )
    ).distinct()

    students_list = list(students_in_month.order_by('first_name', 'last_name'))
    student_ids = [s.id for s in students_list]

    # Tanlangan oy to'lovlari
    payments = MonthlyPayment.objects.filter(student_id__in=student_ids, month=req_month)
    payment_map = {p.student_id: p for p in payments}

    rows = []
    total_rate = 0
    total_paid = 0
    daily_collected = 0
    total_debt = 0
    paid_count = 0
    unpaid_count = 0
    overdue_count = 0
    local_count = 0

    curr_month_str = today.strftime('%Y-%m')

    for idx, s in enumerate(students_list, start=1):
        is_loc = getattr(s, 'is_local', False)
        if is_loc:
            local_count += 1

        grp = s.time_slot.group if s.time_slot else None
        subject_name = grp.subject_name if grp else "-"
        teacher_name = str(grp.teacher) if (grp and grp.teacher) else "-"
        pay = payment_map.get(s.id)

        raw_paid = str(pay.amount_paid if pay else '').strip()
        num_paid = _clean_amount(raw_paid)

        rate_val = s.payment_rate or 0
        total_rate += rate_val

        # ── To'lov sanasi (to'langan sana) ──────────────────────────────────
        pay_date_obj = None
        if pay and pay.payment_date:
            pay_date_obj = pay.payment_date
        elif pay and pay.created_at:
            pay_date_obj = pay.created_at.date()

        pay_date_str = pay_date_obj.strftime('%d.%m.%Y') if pay_date_obj else ""
        pay_date_iso = pay_date_obj.strftime('%Y-%m-%d') if pay_date_obj else ""

        # ── Bugungi tushum (Aynan bugun to'langan to'lovlar) ────────────────
        if pay_date_obj == today and num_paid > 0:
            daily_collected += num_paid

        # ── Oylik tushum (Tanlangan oy uchun barcha haqiqiy to'lovlar) ───────
        total_paid += num_paid

        # ── To'lov muddati (due_date) hisoblash ─────────────────────────────
        is_text_local = raw_paid.lower() in ('mahalla', 'm', 'tekin')
        has_paid_val = (num_paid > 0) or is_text_local or is_loc

        if pay_date_obj and has_paid_val:
            if relativedelta:
                due_date = pay_date_obj + relativedelta(months=1)
            else:
                import calendar as _cal
                yr, mn = pay_date_obj.year, pay_date_obj.month
                mn += 1
                if mn > 12:
                    mn = 1
                    yr += 1
                max_next = _cal.monthrange(yr, mn)[1]
                due_date = date(yr, mn, min(pay_date_obj.day, max_next))
        else:
            joined_d = s.joined_date or today
            due_day = min(joined_d.day, max_day_in_month)
            due_date = date(req_year, req_mon, due_day)

        due_date_str = due_date.strftime('%d.%m.%Y')

        # ── Holat aniqlash ───────────────────────────────────────────────────
        # Tanlangan oy bo'yicha to'lov qilingan bo'lsa 'paid', aks holda 'unpaid'
        if has_paid_val:
            st_code = 'paid'
        else:
            st_code = 'unpaid'

        # ── Kechikish hisobi ─────────────────────────────────────────────────
        is_overdue = False
        delay_days = 0
        if st_code == 'unpaid':
            if req_month < curr_month_str:
                is_overdue = True
                delay_days = max(1, (today - due_date).days)
                overdue_count += 1
            elif today > due_date:
                is_overdue = True
                delay_days = (today - due_date).days
                overdue_count += 1

        # Qarz hisobi: mahalla bo'lsa 0, aks holda tarif - to'langan
        if is_loc or is_text_local:
            debt_val = 0
        else:
            debt_val = max(0, rate_val - num_paid)

        total_debt += debt_val

        if st_code == 'paid':
            paid_count += 1
        else:
            unpaid_count += 1

        status_display = "To'langan" if st_code == 'paid' else ("Muddati o'tgan" if is_overdue else "To'lanmagan")

        joined_str = s.joined_date.strftime('%d.%m.%Y') if s.joined_date else "-"
        phone_str = f"{s.phone_1} {s.phone_2 or ''}".strip()

        row_item = {
            'index': idx,
            'student': s,
            'student_id': s.id,
            'student_name': f"{s.first_name} {s.last_name}",
            'is_local': is_loc,
            'group': grp,
            'group_name': grp.name if grp else "-",
            'group_id': grp.id if grp else None,
            'subject_name': subject_name,
            'teacher_name': teacher_name,
            'joined_date': joined_str,
            # To'lov muddati va kechikish:
            'due_date': due_date,
            'due_date_str': due_date_str,
            'is_overdue': is_overdue,
            'delay_days': delay_days,
            'delay_text': f"{delay_days} kun kechikdi" if is_overdue else "",
            # To'lov qilingan sana va miqdorlar:
            'payment_date': pay_date_str,
            'payment_date_iso': pay_date_iso,
            'payment_rate': rate_val,
            'payment_rate_formatted': f"{rate_val:,}".replace(',', ' '),
            'amount_paid': raw_paid,
            'amount_paid_formatted': f"{num_paid:,}".replace(',', ' ') if num_paid > 0 else (raw_paid if raw_paid else "-"),
            'num_paid': num_paid,
            'status_code': st_code,
            'status_display': status_display,
            'phone': phone_str,
            'notes': pay.notes if pay else "",
            'payment_id': pay.id if pay else None,
            # Qarz hisobi: to'lov tarifi - to'langan miqdor
            'debt': debt_val,
            'debt_formatted': f"{debt_val:,}".replace(',', ' '),
        }

        # Holat filtri:
        if status_filter == 'paid' and st_code != 'paid':
            continue
        if status_filter == 'unpaid' and st_code != 'unpaid':
            continue
        if status_filter == 'overdue' and not is_overdue:
            continue
        if status_filter == 'local' and not (is_loc or is_text_local):
            continue

        rows.append(row_item)

    # Indekslarni yangilash
    for i, r in enumerate(rows, start=1):
        r['index'] = i

    stats = {
        'total_students': len(students_list),
        'paid_count': paid_count,
        'unpaid_count': unpaid_count,
        'overdue_count': overdue_count,
        'local_count': local_count,
        'total_rate': total_rate,
        'total_rate_formatted': f"{total_rate:,}".replace(',', ' '),
        'total_paid': total_paid,
        'total_paid_formatted': f"{total_paid:,}".replace(',', ' '),
        'debt_sum': total_debt,
        'debt_sum_formatted': f"{total_debt:,}".replace(',', ' '),
        # Kunlik tushum:
        'daily_collected': daily_collected,
        'daily_collected_formatted': f"{daily_collected:,}".replace(',', ' '),
        'req_year': req_year,
    }

    return rows, stats


@admin_required
def admin_payments_all(request):
    """Barcha guruhlar bo'yicha umumiy to'lovlar sahifasi."""
    if not _admin_only(request):
        return redirect('home')

    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    group_id = request.GET.get('group_id', '')
    status_filter = request.GET.get('status', 'all')
    search_q = request.GET.get('q', '').strip()

    groups = Group.objects.select_related('teacher').order_by('name')

    students_qs = Student.objects.select_related(
        'time_slot__group__teacher', 'time_slot__group'
    )

    if group_id and group_id.isdigit():
        students_qs = students_qs.filter(time_slot__group_id=int(group_id))

    if search_q:
        words = search_q.split()
        q_filter = Q()
        for w in words:
            q_filter |= Q(first_name__icontains=w) | Q(last_name__icontains=w) | Q(phone_1__icontains=w) | Q(phone_2__icontains=w)
        students_qs = students_qs.filter(q_filter)

    rows, stats = _build_payments_data(
        students_qs, req_month, status_filter,
        is_group_specific=bool(group_id and group_id.isdigit())
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
    if not _admin_only(request):
        return redirect('home')

    group = get_object_or_404(Group.objects.select_related('teacher'), id=group_id)
    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    status_filter = request.GET.get('status', 'all')
    search_q = request.GET.get('q', '').strip()

    students_qs = Student.objects.filter(time_slot__group=group).select_related('time_slot')

    if search_q:
        words = search_q.split()
        q_filter = Q()
        for w in words:
            q_filter |= Q(first_name__icontains=w) | Q(last_name__icontains=w) | Q(phone_1__icontains=w) | Q(phone_2__icontains=w)
        students_qs = students_qs.filter(q_filter)

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
    """
    Admin tomonidan o'quvchi to'lovini to'g'ridan-to'g'ri (inline) saqlash / yangilash.

    Mantiq:
      1. Tanlangan oy uchun faqat to'lagan miqdorni bazaga yozadi.
      2. To'langan sana — sana input olib tashlandi, doim bugungi sana (date.today()) saqlanadi.
      3. Agar amount > 0 bo'lsa, status = 'paid'; aks holda 'unpaid'.
    """
    if not _admin_only(request):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
            return JsonResponse({'status': 'error', 'msg': 'Ruxsat berilmagan'}, status=403)
        messages.error(request, "Ruxsat berilmagan!")
        return redirect('home')

    if request.method != 'POST':
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
            return JsonResponse({'status': 'error', 'msg': "Noto'g'ri so'rov"}, status=400)
        return redirect('admin_payments_all')

    student_id = request.POST.get('student_id')
    month = request.POST.get('month', '').strip()
    amount_raw = request.POST.get('amount') or request.POST.get('amount_paid') or '0'
    notes = request.POST.get('notes', '').strip()

    # ── Oyni aniqlash ───────────────────────────────────────────────────────
    # Agar oy yuborilmagan bo'lsa, joriy oydan olinadi
    if not month:
        month = date.today().strftime('%Y-%m')

    if not student_id:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
            return JsonResponse({'status': 'error', 'msg': "O'quvchi ko'rsatilmadi"}, status=400)
        messages.error(request, "O'quvchi tanlanmadi!")
        return redirect('admin_payments_all')

    student = get_object_or_404(Student, id=student_id)

    # ── Miqdorni aniqlash ───────────────────────────────────────────────────
    amount_raw_str = str(amount_raw).strip()
    clean_digits = amount_raw_str.replace(' ', '').replace("'", '').replace(',', '')
    if clean_digits.isdigit():
        amount_num = int(clean_digits)
        amount_to_save = str(amount_num)
        new_status = 'paid' if amount_num > 0 else 'unpaid'
    else:
        amount_num = 0
        amount_to_save = amount_raw_str
        new_status = 'paid' if amount_to_save else 'unpaid'

    # ── To'langan sana: doim bugungi sana (input olib tashlandi) ────────────
    p_date = date.today()

    # ── To'lovni BAZAGA SAQLASH ─────────────────────────────────────────────
    mp, created = MonthlyPayment.objects.update_or_create(
        student=student,
        month=month,
        defaults={
            'amount_paid': amount_to_save,
            'payment_date': p_date,
            'status': new_status,
            'notes': notes,
        }
    )

    # ── AJAX javobi ─────────────────────────────────────────────────────────
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
        saved_num = int(mp.amount_paid) if str(mp.amount_paid).isdigit() else 0
        rate_val = student.payment_rate or 0

        # due_date hisoblash — to'lov qilingan bo'lsa: p_date + 1 oy
        if saved_num > 0 and mp.payment_date:
            if relativedelta:
                due_date_obj = mp.payment_date + relativedelta(months=1)
            else:
                yr, mn = mp.payment_date.year, mp.payment_date.month
                mn += 1
                if mn > 12:
                    mn = 1
                    yr += 1
                import calendar as _cal
                max_next = _cal.monthrange(yr, mn)[1]
                due_date_obj = date(yr, mn, min(mp.payment_date.day, max_next))
            due_date_str = due_date_obj.strftime('%d.%m.%Y')
        else:
            due_date_str = ''

        today = date.today()
        is_paid_status = (saved_num > 0 and mp.payment_date and today <= (due_date_obj if saved_num > 0 and mp.payment_date else today))
        status_display = "To'langan" if is_paid_status else "Qarzdor"

        return JsonResponse({
            'status': 'success',
            'payment_id': mp.id,
            'amount_paid': mp.amount_paid,
            'amount_paid_num': saved_num,
            'amount_paid_formatted': f"{saved_num:,}".replace(',', ' ') if saved_num > 0 else '0',
            'payment_date': mp.payment_date.strftime('%d.%m.%Y') if mp.payment_date else '',
            'payment_date_iso': mp.payment_date.strftime('%Y-%m-%d') if mp.payment_date else '',
            'due_date': due_date_str,
            'status_code': mp.status,
            'status_display': status_display,
            'notes': mp.notes,
            'rate': rate_val,
            'debt': 0 if is_paid_status else rate_val,
            'debt_formatted': "0" if is_paid_status else f"{rate_val:,}".replace(',', ' '),
        })

    # ── Oddiy redirect javob ─────────────────────────────────────────────────
    formatted_amount = f"{amount_num:,}".replace(',', ' ')
    if amount_num > 0:
        messages.success(
            request,
            f"✅ {student.first_name} {student.last_name}: {formatted_amount} so'm "
            f"({month}) uchun muvaffaqiyatli saqlandi!"
        )
    elif amount_to_save and amount_to_save not in ('0', ''):
        messages.success(
            request,
            f"✅ {student.first_name} {student.last_name}: '{amount_to_save}' "
            f"({month}) uchun muvaffaqiyatli saqlandi!"
        )
    else:
        messages.warning(
            request,
            f"⚠️ {student.first_name} {student.last_name}: {month} uchun to'lov bekor qilindi (0 so'm)."
        )
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or 'admin_payments_all'
    return redirect(next_url)


@admin_required
def admin_payment_delete(request, id):
    """To'lov yozuvini bekor qilish/o'chirish."""
    if not _admin_only(request):
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'status': 'error', 'msg': 'Ruxsat berilmagan'}, status=403)
        messages.error(request, "Ruxsat berilmagan!")
        return redirect('home')

    mp = get_object_or_404(MonthlyPayment, id=id)
    student_name = f"{mp.student.first_name} {mp.student.last_name}"
    mp.delete()
    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JsonResponse({'status': 'success'})
    messages.success(request, f"{student_name} to'lov ma'lumoti bekor qilindi.")
    next_url = request.GET.get('next') or request.POST.get('next') or request.META.get('HTTP_REFERER') or 'admin_payments_all'
    return redirect(next_url)


@admin_required
def export_payments_excel(request):
    """To'lovlar jadvalini professional Excel fayl (.xlsx) shaklida eksport qilish."""
    if not _admin_only(request):
        return redirect('home')

    from .payment_excel import generate_payments_excel

    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    group_id = request.GET.get('group_id', '')
    status_filter = request.GET.get('status', 'all')
    is_local_param = request.GET.get('is_local', '')

    if is_local_param in ('1', 'true', 'True') or status_filter == 'local':
        status_filter = 'local'

    group = None
    is_grp = bool(group_id and group_id.isdigit())
    if is_grp:
        group = get_object_or_404(Group.objects.select_related('teacher'), id=int(group_id))
        students_qs = Student.objects.filter(time_slot__group=group).select_related('time_slot')
        base_title = f"Guruh To'lovlari — {group.name}"
        safe_name = "".join(c for c in group.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        base_filename = f"Toluvlar_{safe_name}"
        group_info = f"Fan: {group.subject_name} | O'qituvchi: {group.teacher or '—'}"
    else:
        students_qs = Student.objects.select_related('time_slot__group__teacher', 'time_slot__group')
        base_title = "Barcha Guruhlar Umumiy To'lovlar Jadvali"
        base_filename = "Humo_Umumiy_Toluvlar"
        group_info = "Barcha guruhlar"

    # Statusga mos sarlavha va fayl nomi
    if status_filter == 'paid':
        type_label = "To'liq To'laganlar"
        filename = f"{base_filename}_Tolaganlar_{req_month}.xlsx"
    elif status_filter == 'overdue':
        type_label = "Kechikkanlar (Muddati O'tganlar)"
        filename = f"{base_filename}_Kechikkanlar_{req_month}.xlsx"
    elif status_filter == 'unpaid':
        type_label = "Qarzdorlar (Umuman to'lamaganlar)"
        filename = f"{base_filename}_Qarzdorlar_{req_month}.xlsx"
    elif status_filter == 'partial':
        type_label = "Qisman To'laganlar"
        filename = f"{base_filename}_Qisman_Tolaganlar_{req_month}.xlsx"
    elif status_filter == 'local':
        type_label = "Mahalladan Kelganlar"
        filename = f"{base_filename}_Mahalladan_{req_month}.xlsx"
    elif status_filter == 'yearly':
        type_label = f"Yillik Moliyaviy Hisobot ({req_month[:4]})"
        filename = f"{base_filename}_Yillik_{req_month[:4]}.xlsx"
    else:
        type_label = "Barcha O'quvchilar"
        filename = f"{base_filename}_{req_month}.xlsx"

    title = f"{base_title} ({type_label})"
    subtitle = f"{group_info} | Holat: {type_label} | Davr: {req_month} | Yuklangan: {date.today().strftime('%d.%m.%Y')}"

    # Agar yillik tanlangan bo'lsa barcha o'quvchilar ro'yxati olinadi
    filter_for_data = 'all' if status_filter == 'yearly' else status_filter
    rows, stats = _build_payments_data(students_qs, req_month, status_filter=filter_for_data, is_group_specific=is_grp)

    excel_file = generate_payments_excel(rows, title=title, subtitle=subtitle, stats=stats)

    response = HttpResponse(
        excel_file.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@admin_required
def payment_mark(request):
    """Eski payment_mark endpointi bilan moslik (backward compatibility)."""
    return admin_payment_save(request)


# ─────────────────────────────────────────────
# TEACHER VIEWS
# ─────────────────────────────────────────────

def teacher_login(request):
    if request.user.is_authenticated and request.user.role == 'teacher':
        return redirect('teacher_dashboard')
    error = None
    if request.method == 'POST':
        u = request.POST.get('login', '').strip()
        p = request.POST.get('password', '').strip()
        user = authenticate(request, username=u, password=p)
        if user and user.role == 'teacher':
            login(request, user)
            return redirect('teacher_dashboard')
        error = "Login yoki parol noto'g'ri."
    return render(request, 'teacher_login.html', {'error': error})


@login_required(login_url='/teacher/login/')
def teacher_dashboard(request):
    if request.user.role != 'teacher':
        return redirect('home')
    teacher = request.user.teacher_profile
    groups = teacher.groups.prefetch_related('time_slots__students')
    
    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    try:
        y, m = map(int, req_month.split('-'))
    except:
        y, m = date.today().year, date.today().month
        req_month = f"{y}-{m:02d}"

    num_days = calendar.monthrange(y, m)[1]
    weekdays_uz = ['Du', 'Se', 'Chor', 'Pay', 'Ju', 'Sha', 'Yak']
    today_date = date.today()

    days_data = []
    for d in range(1, num_days + 1):
        cur_d = date(y, m, d)
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

    t_month_end = date(y, m, num_days)

    def _teacher_student_in_month(st):
        if st.joined_date and st.joined_date > t_month_end:
            return False
        if st.left_date:
            return st.left_date > t_month_end
        return st.is_active

    student_ids = []
    for g in groups:
        for ts in g.time_slots.all():
            for s in ts.students.all():
                if _teacher_student_in_month(s):
                    student_ids.append(s.id)

    attendance_map = {sid: {} for sid in student_ids}
    if student_ids:
        start_d = date(y, m, 1)
        end_d = date(y, m, num_days)
        attendances = Attendance.objects.filter(student_id__in=student_ids, date__range=(start_d, end_d))
        for a in attendances:
            attendance_map[a.student_id][a.date.day] = a.status

    # Annotate students with smart schedule attendance matrix
    from .schedule_helper import parse_schedule_weekdays
    for g in groups:
        for ts in g.time_slots.all():
            ts_weekdays = parse_schedule_weekdays(ts.days)
            ts.active_students = [s for s in ts.students.all() if _teacher_student_in_month(s)]
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
    # Check if student belongs to this teacher
    if not (student.time_slot and student.time_slot.group and student.time_slot.group.teacher and student.time_slot.group.teacher.user == request.user):
        return JsonResponse({'status': 'error', 'msg': 'Ruxsat yo\'q'}, status=403)

    if status in ('present', 'absent'):
        att, _ = Attendance.objects.update_or_create(
            student=student, date=req_date,
            defaults={'status': status},
        )
        return JsonResponse({'status': 'success', 'attendance': att.status})
    else:
        # Bo'shatish yoki o'chirish holati
        Attendance.objects.filter(student=student, date=req_date).delete()
        return JsonResponse({'status': 'success', 'attendance': ''})


@login_required(login_url='/teacher/login/')
def teacher_student_create(request, timeslot_id):
    if request.user.role != 'teacher':
        return redirect('home')
    timeslot = get_object_or_404(TimeSlot, id=timeslot_id)
    # Check if timeslot belongs to this teacher
    if not (timeslot.group and timeslot.group.teacher and timeslot.group.teacher.user == request.user):
        return redirect('teacher_dashboard')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        age = request.POST.get('age', '').strip()
        phone_1 = request.POST.get('phone_1', '').strip()
        phone_2 = request.POST.get('phone_2', '').strip()
        photo = request.FILES.get('photo')

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning (Kirill harflari taqiqlangan)!")
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
                is_local=is_local_val
            )
            if age.isdigit():
                student.age = int(age)
            if photo:
                opt_photo, err = optimize_student_photo(photo)
                student.photo = opt_photo if opt_photo else photo
            student.save()

            messages.success(request, f"O'quvchi '{first_name} {last_name}' muvaffaqiyatli qo'shildi.")
            return redirect('teacher_dashboard')
    return render(request, 'teacher_student_create.html', {'timeslot': timeslot})


@login_required(login_url='/teacher/login/')
def teacher_student_delete(request, id):
    """O'qituvchi o'z guruhidagi o'quvchini xavfsiz o'chirib, arxivga yuborishi."""
    if request.user.role != 'teacher':
        return redirect('home')
    student = get_object_or_404(Student, id=id)
    
    # Ensure student belongs to teacher
    if not (student.time_slot and student.time_slot.group and student.time_slot.group.teacher and student.time_slot.group.teacher.user == request.user):
        messages.error(request, "Bu o'quvchi sizning guruhingizga biriktirilmagan.")
        return redirect('teacher_dashboard')
        
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if not reason:
            messages.error(request, "O'quvchini o'chirish uchun sabab kiritish majburiy!")
            return redirect('teacher_dashboard')
            
        if contains_cyrillic(reason):
            messages.error(request, "O'chirish sababida faqat lotin alifbosidagi harflardan foydalaning (Kirill taqiqlangan)!")
            return redirect('teacher_dashboard')
            
        group_name = student.time_slot.group.name if (student.time_slot and student.time_slot.group) else "Noma'lum"
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
        # Soft delete: o'quvchi o'chirilmaydi, faqat left_date va is_active belgilanadi
        student.is_active = False
        student.left_date = date.today()
        student.save()
        messages.success(request, f"O'quvchi '{st_full_name}' arxivga muvaffaqiyatli o'tkazildi.")
        return redirect('teacher_dashboard')
    return redirect('teacher_dashboard')


# ─────────────────────────────────────────────
# PARENT VIEW
# ─────────────────────────────────────────────

def parent_view(request):
    groups = Group.objects.prefetch_related(
        'time_slots__students__attendances'
    ).select_related('teacher')
    return render(request, 'parent_view.html', {'groups': groups})


# ─────────────────────────────────────────────
# ADMIN: Toggle Teacher Edit Permission
# ─────────────────────────────────────────────

@admin_required
def admin_teacher_toggle_edit(request, id):
    """Admin o'qituvchiga o'quvchini tahrirlash ruxsatini yoqadi/o'chiradi (toggle)."""
    if not _admin_only(request):
        return redirect('home')
    teacher = get_object_or_404(Teacher, id=id)
    teacher.can_edit_students = not bool(teacher.can_edit_students)
    teacher.save(update_fields=['can_edit_students'])
    status_text = "yoqildi ✅" if teacher.can_edit_students else "o'chirildi ❌"
    messages.success(request, f"{teacher.first_name} {teacher.last_name} uchun tahrirlash ruxsati {status_text}")
    return redirect('admin_dashboard')


@admin_required
def admin_teacher_toggle_all_edit(request):
    """Barcha o'qituvchilar uchun tahrirlash ruxsatini bir yo'la yoqish/o'chirish (GLOBAL TOGGLE)."""
    if not _admin_only(request):
        return redirect('home')
    if request.method != 'POST':
        return redirect('admin_dashboard')
    action = request.POST.get('action', 'off')  # 'on' yoki 'off'
    new_val = (action == 'on')
    count = Teacher.objects.update(can_edit_students=new_val)
    if new_val:
        messages.success(request, f"Barcha {count} ta o'qituvchi uchun tahrirlash ruxsati YOQILDI ✅")
    else:
        messages.success(request, f"Barcha {count} ta o'qituvchi uchun tahrirlash ruxsati O'CHIRILDI ❌")
    return redirect('admin_dashboard')


# ─────────────────────────────────────────────
# ADMIN: Student Edit
# ─────────────────────────────────────────────

@admin_required
def admin_student_edit(request, id):
    """Admin tomonidan o'quvchi ma'lumotlarini tahrirlash."""
    if not _admin_only(request):
        return redirect('home')
    student = get_object_or_404(Student, id=id)
    next_url = request.GET.get('next') or request.POST.get('next', '')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        age_raw = request.POST.get('age', '').strip()
        phone_1 = request.POST.get('phone_1', '').strip()
        phone_2 = request.POST.get('phone_2', '').strip()
        payment_rate_raw = request.POST.get('payment_rate', '').strip()

        errors = []
        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            errors.append("Ism va familiyada faqat lotin alifbosidan foydalaning!")

        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            student.first_name = first_name or student.first_name
            student.last_name = last_name or student.last_name
            student.age = int(age_raw) if age_raw.isdigit() else student.age
            student.phone_1 = phone_1 or student.phone_1
            student.phone_2 = phone_2
            if payment_rate_raw.isdigit():
                student.payment_rate = int(payment_rate_raw)
            student.is_local = (request.POST.get('is_local') in ('1', 'on', 'true', True))
            student.save()
            messages.success(request, f"O'quvchi ma'lumotlari yangilandi.")
            if next_url:
                return redirect(next_url)
            if student.time_slot:
                return redirect('admin_group_detail', id=student.time_slot.group.id)
            return redirect('admin_dashboard')

    return render(request, 'student_edit.html', {
        'student': student,
        'next': next_url,
        'role': 'admin',
    })


# ─────────────────────────────────────────────
# TEACHER: Student Edit (faqat ruxsat berilgan bo'lsa)
# ─────────────────────────────────────────────

@login_required(login_url='/teacher/login/')
def teacher_student_edit(request, id):
    """O'qituvchi tomonidan o'quvchi ma'lumotlarini tahrirlash (faqat admin ruxsat bersa)."""
    if request.user.role != 'teacher':
        return redirect('home')
    student = get_object_or_404(Student, id=id)

    # Guruhga tegishliligini tekshirish
    if not (student.time_slot and student.time_slot.group and
            student.time_slot.group.teacher and
            student.time_slot.group.teacher.user == request.user):
        messages.error(request, "Bu o'quvchi sizning guruhingizga tegishli emas.")
        return redirect('teacher_dashboard')

    teacher = request.user.teacher_profile
    if not teacher.can_edit_students:
        messages.error(request, "Sizda o'quvchini tahrirlash ruxsati yo'q. Admin bilan bog'laning.")
        return redirect('teacher_dashboard')

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
                student.is_local = (request.POST.get('is_local') in ('1', 'on', 'true', True))
            student.save()
            messages.success(request, f"O'quvchi ma'lumotlari yangilandi.")
            return redirect('teacher_dashboard')

    return render(request, 'student_edit.html', {
        'student': student,
        'next': request.GET.get('next', ''),
        'role': 'teacher',
    })


# ─────────────────────────────────────────────
# SHARED: Student Photo Upload
# ─────────────────────────────────────────────

def student_photo_upload(request, id):
    """O'quvchiga rasm yuklash / yangilash. Admin yoki ruxsat berilgan o'qituvchi."""
    is_admin = bool(request.session.get('admin_authenticated'))
    is_teacher = request.user.is_authenticated and getattr(request.user, 'role', '') == 'teacher'

    if not is_admin and not is_teacher:
        return JsonResponse({'status': 'error', 'msg': 'Ruxsat berilmagan'}, status=403)

    student = get_object_or_404(Student, id=id)

    # O'qituvchi faqat o'z guruhidagi o'quvchini o'zgartira oladi
    if is_teacher and not is_admin:
        if not (student.time_slot and student.time_slot.group and
                student.time_slot.group.teacher and
                student.time_slot.group.teacher.user == request.user):
            return JsonResponse({'status': 'error', 'msg': 'Bu o\'quvchi sizning guruhingizda emas'}, status=403)

    if request.method == 'POST':
        photo = request.FILES.get('photo')
        if not photo:
            return JsonResponse({'status': 'error', 'msg': 'Rasm fayli tanlanmadi'}, status=400)

        # Mobil qurilmalardan (iOS/Android) olingan rasmni optimallashtirish va aylantirish
        opt_photo, err = optimize_student_photo(photo)
        if not opt_photo:
            return JsonResponse({'status': 'error', 'msg': f"Rasm formatini qayta ishlab bo'lmadi: {err or 'Noma\'lum xatolik'}"}, status=400)

        # Eski rasmni diskdan tozalash
        if student.photo:
            try:
                if os.path.exists(student.photo.path):
                    os.remove(student.photo.path)
            except Exception:
                pass

        student.photo.save(opt_photo.name, opt_photo, save=True)
        return JsonResponse({'status': 'success', 'photo_url': student.photo.url})

    return JsonResponse({'status': 'error', 'msg': 'Noto\'g\'ri so\'rov usuli'}, status=405)


from .models import Message

@admin_required
def admin_chat_list(request):
    teachers = Teacher.objects.all()
    teacher_data = []
    for t in teachers:
        unread = Message.objects.filter(sender=t.user, receiver=request.user, is_read=False).count()
        teacher_data.append({'teacher': t, 'unread': unread})
    return render(request, 'admin_chat_list.html', {'teacher_data': teacher_data})

@admin_required
def admin_chat_detail(request, teacher_id):
    teacher = get_object_or_404(Teacher, id=teacher_id)
    t_user = teacher.user
    
    # Mark messages as read
    Message.objects.filter(sender=t_user, receiver=request.user, is_read=False).update(is_read=True)
    
    messages_qs = Message.objects.filter(
        (Q(sender=request.user) & Q(receiver=t_user)) |
        (Q(sender=t_user) & Q(receiver=request.user))
    ).order_by('timestamp')
    
    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        if content:
            Message.objects.create(sender=request.user, receiver=t_user, content=content)
            return redirect('admin_chat_detail', teacher_id=teacher_id)
            
    return render(request, 'admin_chat_detail.html', {
        'chat_user': t_user, 
        'teacher': teacher,
        'chat_messages': messages_qs
    })

@login_required(login_url='/teacher/login/')
def teacher_chat(request):
    if request.user.role != 'teacher':
        return redirect('home')
        
    admin_user = CustomUser.objects.filter(role='admin').first()
    if not admin_user:
        return HttpResponse("Admin topilmadi", status=404)
        
    # Mark messages as read
    Message.objects.filter(sender=admin_user, receiver=request.user, is_read=False).update(is_read=True)
    
    messages_qs = Message.objects.filter(
        (Q(sender=request.user) & Q(receiver=admin_user)) |
        (Q(sender=admin_user) & Q(receiver=request.user))
    ).order_by('timestamp')
    
    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        if content:
            Message.objects.create(sender=request.user, receiver=admin_user, content=content)
            return redirect('teacher_chat')
            
    return render(request, 'teacher_chat.html', {
        'chat_user': admin_user,
        'chat_messages': messages_qs
    })
