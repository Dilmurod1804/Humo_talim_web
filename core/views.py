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

from datetime import date

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
            
        search_results = Student.objects.filter(q_filter).select_related(
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
        Group.objects.create(name=name, teacher=teacher)
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
        # Guruhdagi o'quvchilarni DeletedStudent ga arxivlash
        for ts in group.time_slots.all():
            for student in ts.students.all():
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
                    time_slot_id=None
                )
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

    student_ids = []
    for ts in timeslots:
        for s in ts.students.all():
            if s.is_active:
                student_ids.append(s.id)

    attendance_map = {sid: {} for sid in student_ids}
    if student_ids:
        start_d = date(y, m, 1)
        end_d = date(y, m, num_days)
        attendances = Attendance.objects.filter(student_id__in=student_ids, date__range=(start_d, end_d))
        for a in attendances:
            attendance_map[a.student_id][a.date.day] = a.status

    # To'lovlar xaritasi
    payment_map = {}
    if student_ids:
        payments = MonthlyPayment.objects.filter(student_id__in=student_ids, month=req_month)
        for p in payments:
            payment_map[p.student_id] = p.amount_paid

    # Annotate students with smart schedule attendance matrix
    from .schedule_helper import parse_schedule_weekdays
    for ts in timeslots:
        ts_weekdays = parse_schedule_weekdays(ts.days)
        ts.active_students = [s for s in ts.students.all() if s.is_active]
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
            s.paid_amount = payment_map.get(s.id, 0)

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
            Student.objects.create(
                first_name=ds.first_name or ds.student_name.split()[0] if ds.student_name else "Ism",
                last_name=ds.last_name or (ds.student_name.split()[1] if len(ds.student_name.split()) > 1 else ""),
                age=ds.age or 0,
                phone_1=ds.phone_1 or ds.phones.split(',')[0].strip() if ds.phones else "",
                phone_2=ds.phone_2 or (ds.phones.split(',')[1].strip() if ',' in ds.phones else ""),
                time_slot=ts
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
            student = Student(
                first_name=first_name,
                last_name=last_name,
                phone_1=phone_1,
                phone_2=phone_2,
                time_slot=timeslot
            )
            if age.isdigit():
                student.age = int(age)
            if photo:
                student.photo = photo
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
            time_slot_id=student.time_slot.id if student.time_slot else None
        )
        student.delete()
        messages.success(request, f"O'quvchi arxivga o'tkazildi.")
        next_url = request.POST.get('next') or request.GET.get('next')
        if next_url:
            return redirect(next_url)
        if group_id:
            return redirect('admin_group_detail', id=group_id)
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')

@admin_required
def admin_export_excel(request):
    if not _admin_only(request):
        return redirect('home')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "O'quvchilar Ro'yxati"

    headers = [
        "F.I.O", "Fan (Guruh)", "O'qituvchi", "Kelgan sana", 
        "To'lov tarifi", "To'lov miqdori (Joriy oy)", "Telefon raqami"
    ]
    ws.append(headers)

    # Style the header
    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.font = openpyxl.styles.Font(bold=True)

    curr_month = date.today().strftime('%Y-%m')
    students = Student.objects.select_related('time_slot__group__teacher').filter(is_active=True)
    
    # Pre-fetch payments for current month
    payments = MonthlyPayment.objects.filter(month=curr_month)
    payment_map = {p.student_id: p.amount_paid for p in payments}

    for s in students:
        group_name = s.time_slot.group.name if s.time_slot and s.time_slot.group else "-"
        teacher_name = str(s.time_slot.group.teacher) if s.time_slot and s.time_slot.group and s.time_slot.group.teacher else "-"
        amount = payment_map.get(s.id, 0)
        phone = f"{s.phone_1} {s.phone_2 or ''}".strip()
        
        ws.append([
            f"{s.first_name} {s.last_name}",
            group_name,
            teacher_name,
            s.joined_date.strftime('%Y-%m-%d') if s.joined_date else "",
            s.payment_rate,
            amount,
            phone
        ])

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="Oquvchilar.xlsx"'
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

@admin_required
def payment_mark(request):
    """Admin yoki Teacher tomonidan to'lovni belgilash (AJAX)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': 'Noto\'g\'ri so\'rov'}, status=400)
    
    student_id = request.POST.get('student_id')
    month = request.POST.get('month')
    amount = request.POST.get('amount')
    
    student = get_object_or_404(Student, id=student_id)
    try:
        amount_int = int(amount)
        if amount_int < 0: amount_int = 0
    except:
        return JsonResponse({'status': 'error', 'msg': 'Summa noto\'g\'ri'}, status=400)

    mp, _ = MonthlyPayment.objects.update_or_create(
        student=student, month=month,
        defaults={'amount_paid': amount_int}
    )
    return JsonResponse({'status': 'success', 'amount': mp.amount_paid})


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
        
    student_ids = []
    for g in groups:
        for ts in g.time_slots.all():
            for s in ts.students.all():
                if s.is_active:
                    student_ids.append(s.id)

    attendance_map = {sid: {} for sid in student_ids}
    if student_ids:
        start_d = date(y, m, 1)
        end_d = date(y, m, num_days)
        attendances = Attendance.objects.filter(student_id__in=student_ids, date__range=(start_d, end_d))
        for a in attendances:
            attendance_map[a.student_id][a.date.day] = a.status

    payment_map = {}
    if student_ids:
        payments = MonthlyPayment.objects.filter(student_id__in=student_ids, month=req_month)
        for p in payments:
            payment_map[p.student_id] = p.amount_paid

    # Annotate students with smart schedule attendance matrix
    from .schedule_helper import parse_schedule_weekdays
    for g in groups:
        for ts in g.time_slots.all():
            ts_weekdays = parse_schedule_weekdays(ts.days)
            ts.active_students = [s for s in ts.students.all() if s.is_active]
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
                s.paid_amount = payment_map.get(s.id, 0)

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
            student = Student(
                first_name=first_name,
                last_name=last_name,
                phone_1=phone_1,
                phone_2=phone_2,
                time_slot=timeslot
            )
            if age.isdigit():
                student.age = int(age)
            if photo:
                student.photo = photo
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
        return redirect('teacher_dashboard')
        
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if contains_cyrillic(reason):
            messages.error(request, "O'chirish sababida faqat lotin harflaridan foydalaning!")
            reason = "O'qituvchi tomonidan chiqarildi"
            
        group_name = student.time_slot.group.name if (student.time_slot and student.time_slot.group) else "Noma'lum"
        DeletedStudent.objects.create(
            student_name=f"{student.first_name} {student.last_name}",
            phones=f"{student.phone_1}, {student.phone_2 or ''}".rstrip(', '),
            group_name=group_name,
            reason=reason or "O'qituvchi tomonidan chiqarildi",
            first_name=student.first_name,
            last_name=student.last_name,
            age=student.age,
            phone_1=student.phone_1,
            phone_2=student.phone_2,
            time_slot_id=student.time_slot.id if student.time_slot else None
        )
        student.delete()
        messages.success(request, f"O'quvchi arxivga o'tkazildi.")
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
    if request.method == 'POST':
        teacher.can_edit_students = not teacher.can_edit_students
        teacher.save()
        status_text = "yoqildi ✅" if teacher.can_edit_students else "o'chirildi ❌"
        messages.success(request, f"{teacher.first_name} {teacher.last_name} uchun tahrirlash ruxsati {status_text}")
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
    """O'quvchiga rasm yuklash. Admin yoki ruxsat berilgan o'qituvchi."""
    is_admin = request.session.get('admin_authenticated')
    is_teacher = request.user.is_authenticated and request.user.role == 'teacher'

    if not is_admin and not is_teacher:
        return JsonResponse({'status': 'error', 'msg': 'Ruxsat yo\'q'}, status=403)

    student = get_object_or_404(Student, id=id)

    # O'qituvchi faqat o'z guruhidagi o'quvchini o'zgartira oladi
    if is_teacher and not is_admin:
        teacher = request.user.teacher_profile
        if not (student.time_slot and student.time_slot.group and
                student.time_slot.group.teacher and
                student.time_slot.group.teacher.user == request.user):
            return JsonResponse({'status': 'error', 'msg': 'Ruxsat yo\'q'}, status=403)

    if request.method == 'POST':
        photo = request.FILES.get('photo')
        if photo:
            # Eski rasmni o'chirish
            if student.photo:
                try:
                    import os
                    if os.path.exists(student.photo.path):
                        os.remove(student.photo.path)
                except Exception:
                    pass
            student.photo = photo
            student.save()
            return JsonResponse({'status': 'success', 'photo_url': student.photo.url})
        return JsonResponse({'status': 'error', 'msg': 'Rasm tanlanmadi'}, status=400)

    return JsonResponse({'status': 'error', 'msg': 'Noto\'g\'ri so\'rov'}, status=400)


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
