"""
HUMO O'quv markazi — Admin Views
Admin paneli: guruhlar, o'qituvchilar, dars vaqtlari, o'quvchilar,
to'lovlar, davomat, Excel eksport va chat.
"""
from core.face_service import compare_two_faces
import os
import random
import string
import calendar
from datetime import date, datetime

try:
    from dateutil.relativedelta import relativedelta
except ImportError:
    relativedelta = None

from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

import openpyxl

import base64
import uuid
from decimal import Decimal
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.core.files.base import ContentFile

from .models import (
    Attendance, CustomUser, DeletedStudent, Group, Student,
    Teacher, Manager, FaceIDSettings, FaceIDLog,
    TemporaryStudent, TimeSlot, MonthlyPayment, Message,
    StaffBonus, AuditLog, AdvanceRequest
)
from .forms import TemporaryStudentForm
from .services import optimize_student_photo
from .validators import contains_cyrillic
from .auth_views import (
    admin_required, _admin_only,
    is_admin_user, is_manager_user, is_admin_or_manager,
    admin_only_required, render_403
)
from .schedule_helper import parse_schedule_weekdays
from .payment_excel import generate_payments_excel
from .teacher_views import _student_visible_in_month, _populate_excel_sheet
from .face_service import (
    calculate_haversine_distance, process_face_check_in,
    calculate_staff_salary_summary, match_staff_by_face
)


# ─────────────────────────────────────────────
# HELPERS: Xavfsiz Decimal konversiyasi
# ─────────────────────────────────────────────

def _safe_decimal(value, default='0'):
    """
    Har qanday string qiymatni xavfsiz ravishda Decimal ga o'giradi.
    - Vergul ',' ni nuqta '.' ga almashtiradi (Yevropa formati uchun).
    - Bo'shliqlarni olib tashlaydi.
    - Noto'g'ri format yoki bo'sh qiymat bo'lsa, default qaytaradi.
    """
    try:
        cleaned = str(value).replace(',', '.').strip()
        if not cleaned or cleaned in ('.', '-'):
            return Decimal(default)
        return Decimal(cleaned)
    except Exception:
        return Decimal(default)


# ─────────────────────────────────────────────
# 1. ADMIN DASHBOARD
# ─────────────────────────────────────────────

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

        # O'quvchilarni yashirmaslik: barcha o'quvchilar qidiruvda topiladi
        search_results = Student.objects.filter(q_filter).select_related(
            'time_slot__group__teacher'
        ).prefetch_related('monthly_payments').order_by('first_name', 'last_name')

    groups = Group.objects.select_related('teacher').prefetch_related('time_slots__students')
    teachers = Teacher.objects.filter(is_active=True).prefetch_related('groups')
    managers = Manager.objects.filter(is_active=True)

    today = date.today()
    today_face_logs = FaceIDLog.objects.filter(date=today)
    face_stats = {
        'total_scans': today_face_logs.count(),
        'on_time_count': today_face_logs.filter(is_late=False).count(),
        'late_count': today_face_logs.filter(is_late=True).count(),
        'total_fines': sum(l.fine_amount_deducted for l in today_face_logs),
    }

    return render(request, 'admin_dashboard.html', {
        'groups': groups,
        'teachers': teachers,
        'managers': managers,
        'face_stats': face_stats,
        'search_query': query,
        'search_results': search_results,
    })


# ─────────────────────────────────────────────
# 2. GURUH VA O'QITUVCHI BOSHQARUVI
# ─────────────────────────────────────────────

@admin_required
def admin_group_create(request):
    if not _admin_only(request):
        return redirect('home')
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        teacher_id = request.POST.get('teacher_id')

        if contains_cyrillic(name):
            messages.error(
                request,
                "Guruh nomida faqat lotin alifbosidagi harflardan foydalaning (Kirill harflari taqiqlangan)!"
            )
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
                    time_slot_id=None,
                    student_id=student.id,
                )
                student.is_active = False
                student.left_date = date.today()
                student.save()
        group.delete()
        messages.success(request, f"'{group.name}' guruhi o'chirildi va o'quvchilar arxivlandi.")
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')


@admin_only_required
def admin_teacher_create(request):
    settings_obj = FaceIDSettings.get_settings()
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        monthly_salary = request.POST.get('monthly_salary', '0').strip()
        late_fine_rate = request.POST.get('late_fine_rate', str(settings_obj.default_late_fine_rate)).strip()
        work_start_time = request.POST.get('work_start_time', str(settings_obj.default_work_start_time)).strip()
        work_end_time = request.POST.get('work_end_time', str(settings_obj.default_work_end_time)).strip()
        photo_file = request.FILES.get('photo')

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(
                request,
                "Ism va familiyada faqat lotin alifbosidan foydalaning (Kirill harflari taqiqlangan)!"
            )
            return render(request, 'admin_teacher_create.html', {
                'first_name_val': first_name,
                'last_name_val': last_name,
                'phone_val': phone,
                'default_fine_rate': settings_obj.default_late_fine_rate,
                'default_start_time': settings_obj.default_work_start_time,
                'default_end_time': settings_obj.default_work_end_time,
            })

        login_str = f"t_{first_name.lower()[:3]}_{random.randint(100, 999)}"
        pwd_str = ''.join(random.choices(string.ascii_letters + string.digits, k=8))

        user = CustomUser.objects.create_user(username=login_str, password=pwd_str, role='teacher')

        opt_photo = None
        if photo_file:
            opt_photo, err = optimize_student_photo(photo_file)

        salary_val = _safe_decimal(monthly_salary, '0') if is_admin_user(request) else Decimal('0')
        fine_rate_val = _safe_decimal(late_fine_rate, str(settings_obj.default_late_fine_rate)) if is_admin_user(request) else settings_obj.default_late_fine_rate
        Teacher.objects.create(
            user=user,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            generated_login=login_str,
            generated_password=pwd_str,
            monthly_salary=salary_val,
            late_fine_rate=fine_rate_val,
            work_start_time=work_start_time or '09:00',
            work_end_time=work_end_time or '18:00',
            photo=opt_photo
        )
        messages.success(request, f"O'qituvchi '{first_name} {last_name}' muvaffaqiyatli yaratildi (Face ID fotosi biriktirildi).")
        return redirect('admin_dashboard')
    return render(request, 'admin_teacher_create.html', {
        'default_fine_rate': settings_obj.default_late_fine_rate,
        'default_start_time': settings_obj.default_work_start_time,
        'default_end_time': settings_obj.default_work_end_time,
    })


@admin_only_required
def admin_teacher_delete(request, id):
    teacher = get_object_or_404(Teacher, id=id)
    if request.method == 'POST':
        user = teacher.user
        teacher.delete()
        if user:
            user.delete()
        messages.success(request, "O'qituvchi o'chirildi.")
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')


@admin_only_required
def admin_teacher_toggle_edit(request, id):
    """Admin o'qituvchiga o'quvchini tahrirlash ruxsatini yoqadi/o'chiradi (toggle)."""
    teacher = get_object_or_404(Teacher, id=id)
    teacher.can_edit_students = not bool(teacher.can_edit_students)
    teacher.save(update_fields=['can_edit_students'])
    status_text = "yoqildi ✅" if teacher.can_edit_students else "o'chirildi ❌"
    messages.success(request, f"{teacher.first_name} {teacher.last_name} uchun tahrirlash ruxsati {status_text}")
    return redirect('admin_dashboard')


@admin_only_required
def admin_teacher_toggle_all_edit(request):
    """Barcha o'qituvchilar uchun tahrirlash ruxsatini bir yo'la yoqish/o'chirish (GLOBAL TOGGLE)."""
    if request.method != 'POST':
        return redirect('admin_dashboard')
    action = request.POST.get('action', 'off')
    new_val = (action == 'on')
    count = Teacher.objects.update(can_edit_students=new_val)
    if new_val:
        messages.success(request, f"Barcha {count} ta o'qituvchi uchun tahrirlash ruxsati YOQILDI ✅")
    else:
        messages.success(request, f"Barcha {count} ta o'qituvchi uchun tahrirlash ruxsati O'CHIRILDI ❌")
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


# ─────────────────────────────────────────────
# 3. GURUH JURNALI VA DAVOMAT
# ─────────────────────────────────────────────

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

    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    try:
        y, m = map(int, req_month.split('-'))
    except Exception:
        y, m = date.today().year, date.today().month
        req_month = f"{y}-{m:02d}"

    num_days = calendar.monthrange(y, m)[1]
    weekdays_uz = ['Du', 'Se', 'Chor', 'Pay', 'Ju', 'Sha', 'Yak']
    today_date = timezone.now().date()

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
            'is_past': cur_d < today_date,
            'is_future': cur_d > today_date,
        })

    timeslots = group.time_slots.prefetch_related('students')

    # Guruhga biriktirilgan barcha o'quvchilar doimo ko'rinadi
    student_ids = []
    for ts in timeslots:
        for s in ts.students.all():
            if _student_visible_in_month(s, y, m):
                student_ids.append(s.id)

    attendance_map = {sid: {} for sid in student_ids}
    if student_ids:
        start_d = date(y, m, 1)
        end_d = date(y, m, num_days)
        attendances = Attendance.objects.filter(
            student_id__in=student_ids, date__range=(start_d, end_d)
        )
        for a in attendances:
            attendance_map[a.student_id][a.date.day] = a.status

    for ts in timeslots:
        ts_weekdays = parse_schedule_weekdays(ts.days)
        ts.active_students = [s for s in ts.students.all() if _student_visible_in_month(s, y, m)]
        for s in ts.active_students:
            matrix = []
            for d in days_data:
                is_lesson = (d['weekday_idx'] in ts_weekdays)
                st = attendance_map.get(s.id, {}).get(d['day'], '')
                is_today = d['is_today']
                matrix.append({
                    'day': d['day'],
                    'date_str': d['date_str'],
                    'is_lesson': is_lesson,
                    'status': st,
                    'is_today': is_today,
                    'is_past': d['is_past'],
                    'is_future': d['is_future'],
                    'can_mark': is_lesson and is_today,
                })
            s.attendance_matrix = matrix

    return render(request, 'admin_group_detail.html', {
        'group': group,
        'timeslots': timeslots,
        'current_month': req_month,
        'days_data': days_data,
    })


@admin_required
def admin_attendance_mark(request):
    """Admin uchun davomat belgilash (AJAX). Faqat bugungi kun uchun yo'qlama qilish mumkin."""
    if not _admin_only(request):
        return JsonResponse({'status': 'error', 'msg': "Ruxsat yo'q"}, status=403)
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': "Noto'g'ri so'rov"}, status=400)

    student_id = request.POST.get('student_id')
    status_val = request.POST.get('status', '').strip().lower()
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

    if status_val in ('green', 'blue'):
        status_val = 'present'
    elif status_val in ('red', 'yellow'):
        status_val = 'absent'

    student = get_object_or_404(Student, id=student_id)

    if status_val in ('present', 'absent'):
        try:
            att, _ = Attendance.objects.update_or_create(
                student=student, date=req_date,
                defaults={'status': status_val},
            )
        except ValidationError as ve:
            err_msg = ve.message_dict.get('date', [str(ve)])[0] if hasattr(ve, 'message_dict') else str(ve)
            return JsonResponse({'status': 'error', 'msg': err_msg}, status=400)
        return JsonResponse({'status': 'success', 'attendance': status_val})
    else:
        Attendance.objects.filter(student=student, date=req_date).delete()
        return JsonResponse({'status': 'success', 'attendance': ''})


# ─────────────────────────────────────────────
# 4. O'QUVCHILAR VA ARXIV
# ─────────────────────────────────────────────

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
            messages.error(
                request,
                "Ism va familiyada faqat lotin alifbosidan foydalaning (Kirill harflari taqiqlangan)!"
            )
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

            joined_date_raw = request.POST.get('joined_date', '').strip()
            if joined_date_raw:
                try:
                    parsed_joined = datetime.strptime(joined_date_raw, '%Y-%m-%d').date()
                    if parsed_joined > date.today():
                        messages.error(
                            request,
                            "Kelgan sana kelajakdagi sana bo'lishi mumkin emas! Bugungi yoki o'tgan sanani kiriting."
                        )
                        return render(request, 'admin_student_create.html', {
                            'timeslot': timeslot,
                            'first_name_val': first_name,
                            'last_name_val': last_name,
                            'age_val': age,
                            'phone_1_val': phone_1,
                            'phone_2_val': phone_2,
                        })
                    student.joined_date = parsed_joined
                except Exception:
                    pass

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

        group_id = student.time_slot.group.id if (student.time_slot and student.time_slot.group) else None
        time_slot_id = student.time_slot.id if student.time_slot else None
        group_name = student.time_slot.group.name if (student.time_slot and student.time_slot.group) else "Noma'lum"
        st_full_name = f"{student.first_name} {student.last_name}"

        DeletedStudent.objects.create(
            student_name=st_full_name,
            phones=f"{student.phone_1}, {student.phone_2 or ''}".rstrip(', '),
            group_name=group_name,
            reason=reason or "Admin tomonidan o'chirildi",
            first_name=student.first_name,
            last_name=student.last_name,
            age=student.age,
            phone_1=student.phone_1,
            phone_2=student.phone_2,
            time_slot_id=time_slot_id,
            student_id=student.id,
        )
        student.is_active = False
        student.left_date = date.today()
        student.time_slot = None
        student.save()
        messages.success(request, f"O'quvchi '{st_full_name}' arxivga o'tkazildi.")
        next_url = request.POST.get('next') or request.GET.get('next')
        if next_url:
            return redirect(next_url)
        if group_id:
            return redirect('admin_group_detail', id=group_id)
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')


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
            joined_date_raw = request.POST.get('joined_date', '').strip()
            if joined_date_raw:
                try:
                    parsed_joined = datetime.strptime(joined_date_raw, '%Y-%m-%d').date()
                    if parsed_joined > date.today():
                        messages.error(
                            request,
                            "Kelgan sana kelajakdagi sana bo'lishi mumkin emas! Bugungi yoki o'tgan sanani kiriting."
                        )
                        return render(request, 'student_edit.html', {
                            'student': student,
                            'next': next_url,
                            'role': 'admin',
                        })
                    student.joined_date = parsed_joined
                except Exception:
                    pass
            student.is_local = (request.POST.get('is_local') in ('1', 'on', 'true', True))
            student.save()
            messages.success(request, "O'quvchi ma'lumotlari yangilandi.")
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
# 5. VAQTINCHALIK O'QUVCHILAR (TEMP STUDENTS)
# ─────────────────────────────────────────────

@admin_required
def admin_temp_students(request):
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
    if not _admin_only(request):
        return redirect('home')
    if request.method == 'POST':
        temp = get_object_or_404(TemporaryStudent, id=id)
        temp.delete()
    return redirect('admin_temp_students')


# ─────────────────────────────────────────────
# 6. TO'LOVLAR MANTIQI (PAYMENTS & TEXT PAYMENTS FIX)
# ─────────────────────────────────────────────

def _clean_amount(val) -> int:
    """
    To'lov qiymatidan sof raqamni ajratib oladi.
    Agar to'lov qiymati matn ('mahalla', 'B.U' va h.k.) yoki bo'sh bo'lsa 0 qaytaradi.
    """
    if not val:
        return 0
    s = str(val).replace(' ', '').replace("'", '').replace(',', '').strip()
    return int(s) if s.isdigit() else 0


def _is_text_payment(val) -> bool:
    """
    To'lov katagiga matn kiritilganligini tekshiradi (masalan, 'mahalla', 'B.U', 'tekin' va h.k.).
    Bunday holatda o'quvchi qarzdor emas, balki to'langan deb baholanadi.
    """
    if not val:
        return False
    s = str(val).strip()
    if not s or s in ('0', '-', 'None'):
        return False
    digits = s.replace(' ', '').replace("'", '').replace(',', '')
    return not digits.isdigit()


def _is_mahalla_student(student, pay=None) -> bool:
    """
    O'quvchi mahalla toifasiga kiradimi yoki to'lov sharti mahalla ekanligini tekshiradi:
      1. Student.is_local == True yoki Student.is_mahalla == True
      2. To'lov summasiga (amount_paid) 'mahalla' yozilgan
      3. To'lov izohiga (notes) 'mahalla' yozilgan
    """
    if getattr(student, 'is_local', False) or getattr(student, 'is_mahalla', False):
        return True
    if pay:
        raw_amt = str(getattr(pay, 'amount_paid', '') or '').strip().lower()
        if 'mahalla' in raw_amt:
            return True
        raw_notes = str(getattr(pay, 'notes', '') or '').strip().lower()
        if 'mahalla' in raw_notes:
            return True
    return False


def _build_payments_data(students_qs, req_month, status_filter='all', is_group_specific=False):
    """
    O'quvchilar va ularning to'lovlari bo'yicha ma'lumotlar ro'yxatini shakllantiradi.
    Mantiq:
      1. O'quvchi guruhga biriktirilgan ekan, to'lovlar jadvalida doimo ko'rinib turadi.
         Hech qanday sun'iy joined_date, left_date yoki is_active cheklovlari qo'llanmaydi.
      2. "Mahalla" toifasidagi o'quvchilar (is_local=True, is_mahalla=True yoki to'lov sharti mahalla bo'lganlar):
         - QARZDORLAR (unpaid) ro'yxatiga MUTLAQO qo'shilmaydi
         - Umumiy qarzdorlar soniga (unpaid_count) sanalmaydi
         - Kechikkanlar (overdue) ro'yxatiga va soniga sanalmaydi
         - Qarz summasi har doim 0 so'm hisoblanadi
      3. Matn kiritilganda ('mahalla', 'B.U' va h.k.):
         - Qarzdorlik: 0 so'm (Qarzdor deb hisoblanmaydi)
    """
    today = date.today()
    try:
        req_year, req_mon = map(int, req_month.split('-'))
    except Exception:
        req_year, req_mon = today.year, today.month
        req_month = f"{req_year:04d}-{req_mon:02d}"

    max_day_in_month = calendar.monthrange(req_year, req_mon)[1]

    # Faqat faol o'quvchilar ro'yxati (Arxivlangan, ketgan yoki DeletedStudent bo'lganlar hisobga olinmaydi)
    deleted_ids = DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)
    students_list = list(
        students_qs
        .filter(is_active=True, left_date__isnull=True)
        .exclude(id__in=deleted_ids)
        .distinct()
        .order_by('first_name', 'last_name')
    )
    student_ids = [s.id for s in students_list]

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

    total_cash = 0
    total_terminal = 0
    total_payme = 0
    total_click = 0
    count_cash = 0
    count_terminal = 0
    count_payme = 0
    count_click = 0

    curr_month_str = today.strftime('%Y-%m')

    for idx, s in enumerate(students_list, start=1):
        pay = payment_map.get(s.id)

        raw_paid = str(pay.amount_paid if pay else '').strip()
        num_paid = _clean_amount(raw_paid)
        has_text_payment = _is_text_payment(raw_paid)

        # Mahalla o'quvchisini aniqlash (is_local, is_mahalla yoki to'lov sharti mahalla)
        is_mahalla = _is_mahalla_student(s, pay)
        is_loc = is_mahalla
        if is_mahalla:
            local_count += 1

        grp = s.time_slot.group if s.time_slot else None
        subject_name = grp.subject_name if grp else "-"
        teacher_name = str(grp.teacher) if (grp and grp.teacher) else "-"

        rate_val = s.payment_rate or 0
        total_rate += rate_val

        # To'lov sanasi
        pay_date_obj = None
        if pay and pay.payment_date:
            pay_date_obj = pay.payment_date
        elif pay and pay.created_at:
            pay_date_obj = pay.created_at.date()

        pay_date_str = pay_date_obj.strftime('%d.%m.%Y') if pay_date_obj else ""
        pay_date_iso = pay_date_obj.strftime('%Y-%m-%d') if pay_date_obj else ""

        # Bugungi tushum
        if pay_date_obj == today and num_paid > 0:
            daily_collected += num_paid

        # Oylik tushum
        total_paid += num_paid

        # To'lov turlari bo'yicha tushumlar hisobi
        p_type = getattr(pay, 'payment_type', 'cash') if pay else 'cash'
        if not p_type:
            p_type = 'cash'

        if num_paid > 0:
            if p_type == 'terminal':
                total_terminal += num_paid
                count_terminal += 1
            elif p_type == 'payme':
                total_payme += num_paid
                count_payme += 1
            elif p_type == 'click':
                total_click += num_paid
                count_click += 1
            else:
                total_cash += num_paid
                count_cash += 1

        # To'lov muddati (due_date)
        has_paid_val = (num_paid > 0) or has_text_payment or is_mahalla

        if pay_date_obj and has_paid_val:
            if relativedelta:
                due_date = pay_date_obj + relativedelta(months=1)
            else:
                yr, mn = pay_date_obj.year, pay_date_obj.month
                mn += 1
                if mn > 12:
                    mn = 1
                    yr += 1
                max_next = calendar.monthrange(yr, mn)[1]
                due_date = date(yr, mn, min(pay_date_obj.day, max_next))
        else:
            joined_d = s.joined_date or today
            due_day = min(joined_d.day, max_day_in_month)
            due_date = date(req_year, req_mon, due_day)

        due_date_str = due_date.strftime('%d.%m.%Y')

        # Holat aniqlash (Mahalla o'quvchisi hech qachon 'unpaid' bo'lmaydi)
        if is_mahalla:
            st_code = 'local'
        elif has_paid_val:
            st_code = 'paid'
        else:
            st_code = 'unpaid'

        # Kechikish hisobi (Mahalla o'quvchilari kechikkan deb hisoblanmaydi)
        is_overdue = False
        delay_days = 0
        if not is_mahalla and st_code == 'unpaid':
            if req_month < curr_month_str:
                is_overdue = True
                delay_days = max(1, (today - due_date).days)
                overdue_count += 1
            elif today > due_date:
                is_overdue = True
                delay_days = (today - due_date).days
                overdue_count += 1

        # Qarz hisobi: matnli to'lov ('mahalla', 'B.U' va h.k.) yoki mahalliy bo'lsa QARZ 0!
        if is_mahalla or has_text_payment:
            debt_val = 0
        elif num_paid > 0:
            debt_val = max(0, rate_val - num_paid)
        else:
            debt_val = rate_val

        total_debt += debt_val

        # Status va hisoblagichlar:
        # Mahalla o'quvchilari qarzdorlar soniga (unpaid_count) MUTLAQO sanalmasin!
        if is_mahalla:
            if num_paid > 0:
                paid_count += 1
                status_display = f"To'langan ({raw_paid})"
            elif has_text_payment and raw_paid.lower() != 'mahalla':
                status_display = f"Mahalla ({raw_paid})"
            else:
                status_display = "Mahalla"
        elif st_code == 'paid':
            paid_count += 1
            status_display = f"To'langan ({raw_paid})" if has_text_payment else "To'langan"
        else:
            unpaid_count += 1
            status_display = "Muddati o'tgan" if is_overdue else "To'lanmagan"

        joined_str = s.joined_date.strftime('%d.%m.%Y') if s.joined_date else "-"
        joined_iso = s.joined_date.strftime('%Y-%m-%d') if s.joined_date else ""
        phone_str = f"{s.phone_1} {s.phone_2 or ''}".strip()

        row_item = {
            'index': idx,
            'student': s,
            'student_id': s.id,
            'student_name': f"{s.first_name} {s.last_name}",
            'is_local': is_loc,
            'is_mahalla': is_mahalla,
            'group': grp,
            'group_name': grp.name if grp else "-",
            'group_id': grp.id if grp else None,
            'subject_name': subject_name,
            'teacher_name': teacher_name,
            'joined_date': joined_str,
            'joined_date_iso': joined_iso,
            'due_date': due_date,
            'due_date_str': due_date_str,
            'is_overdue': is_overdue,
            'delay_days': delay_days,
            'delay_text': f"{delay_days} kun kechikdi" if is_overdue else "",
            'payment_date': pay_date_str,
            'payment_date_iso': pay_date_iso,
            'payment_rate': rate_val,
            'payment_rate_formatted': f"{rate_val:,}".replace(',', ' '),
            'amount_paid': raw_paid,
            'amount_paid_formatted': f"{num_paid:,}".replace(',', ' ') if num_paid > 0 else (raw_paid if raw_paid else "-"),
            'num_paid': num_paid,
            'has_text_payment': has_text_payment,
            'status_code': st_code,
            'status_display': status_display,
            'phone': phone_str,
            'notes': pay.notes if pay else "",
            'payment_id': pay.id if pay else None,
            'payment_type': pay.payment_type if (pay and getattr(pay, 'payment_type', None)) else 'cash',
            'payment_type_display': pay.get_payment_type_display() if (pay and hasattr(pay, 'get_payment_type_display')) else "Naqd pul (Cash)",
            'debt': debt_val,
            'debt_formatted': f"{debt_val:,}".replace(',', ' '),
        }

        # Filtrlash: Mahalla o'quvchilari Qarzdorlar (unpaid) ro'yxatidan MUTLAQO chiqarilsin
        if status_filter == 'unpaid':
            if is_mahalla or st_code != 'unpaid':
                continue
        elif status_filter == 'paid':
            if st_code != 'paid' or (is_mahalla and num_paid == 0):
                continue
        elif status_filter == 'overdue':
            if is_mahalla or not is_overdue:
                continue
        elif status_filter == 'local':
            if not (is_mahalla or has_text_payment):
                continue

        rows.append(row_item)

    for i, r in enumerate(rows, start=1):
        r['index'] = i

    pct_cash = round((total_cash / total_paid) * 100) if total_paid > 0 else 0
    pct_terminal = round((total_terminal / total_paid) * 100) if total_paid > 0 else 0
    pct_payme = round((total_payme / total_paid) * 100) if total_paid > 0 else 0
    pct_click = round((total_click / total_paid) * 100) if total_paid > 0 else 0

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
        'total_cash': total_cash,
        'total_cash_formatted': f"{total_cash:,}".replace(',', ' '),
        'total_terminal': total_terminal,
        'total_terminal_formatted': f"{total_terminal:,}".replace(',', ' '),
        'total_payme': total_payme,
        'total_payme_formatted': f"{total_payme:,}".replace(',', ' '),
        'total_click': total_click,
        'total_click_formatted': f"{total_click:,}".replace(',', ' '),
        'count_cash': count_cash,
        'count_terminal': count_terminal,
        'count_payme': count_payme,
        'count_click': count_click,
        'pct_cash': pct_cash,
        'pct_terminal': pct_terminal,
        'pct_payme': pct_payme,
        'pct_click': pct_click,
        'debt_sum': total_debt,
        'debt_sum_formatted': f"{total_debt:,}".replace(',', ' '),
        'daily_collected': daily_collected,
        'daily_collected_formatted': f"{daily_collected:,}".replace(',', ' '),
        'req_year': req_year,
    }

    return rows, stats


def get_payment_type_aggregates(month=None, start_date=None, end_date=None):
    """
    To'lov turlari bo'yicha tushumlarni alohida hisoblab beruvchi yordamchi funksiya.
    Django ORM va to'lov modelidan foydalanib:
    - Naqd pul (cash)
    - Terminal (terminal)
    - Payme (payme)
    - Click (click)
    tushumlarini jami summasi, to'lovlar soni va foizini qaytaradi.
    """
    qs = MonthlyPayment.objects.all()
    if month:
        qs = qs.filter(month=month)
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
            ptype = p.payment_type if (hasattr(p, 'payment_type') and p.payment_type in totals) else 'cash'
            totals[ptype] += num
            counts[ptype] += 1
            total_all += num

    return {
        'total_cash': totals['cash'],
        'total_cash_formatted': f"{totals['cash']:,}".replace(',', ' '),
        'total_terminal': totals['terminal'],
        'total_terminal_formatted': f"{totals['terminal']:,}".replace(',', ' '),
        'total_payme': totals['payme'],
        'total_payme_formatted': f"{totals['payme']:,}".replace(',', ' '),
        'total_click': totals['click'],
        'total_click_formatted': f"{totals['click']:,}".replace(',', ' '),
        'count_cash': counts['cash'],
        'count_terminal': counts['terminal'],
        'count_payme': counts['payme'],
        'count_click': counts['click'],
        'pct_cash': round((totals['cash'] / total_all) * 100) if total_all > 0 else 0,
        'pct_terminal': round((totals['terminal'] / total_all) * 100) if total_all > 0 else 0,
        'pct_payme': round((totals['payme'] / total_all) * 100) if total_all > 0 else 0,
        'pct_click': round((totals['click'] / total_all) * 100) if total_all > 0 else 0,
        'total_all': total_all,
        'total_all_formatted': f"{total_all:,}".replace(',', ' '),
    }


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

    deleted_ids = DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)
    students_qs = Student.objects.filter(
        is_active=True,
        left_date__isnull=True,
    ).exclude(
        id__in=deleted_ids
    ).select_related(
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

    # Qarzdorlar filtri: Mahalla (is_local=True), arxiv/ketganlar va to'lov qilganlar QuerySet'dan to'liq chiqarib tashlansin
    if status_filter == 'unpaid':
        mahalla_pay_ids = MonthlyPayment.objects.filter(
            month=req_month
        ).filter(
            Q(amount_paid__icontains='mahalla') | Q(notes__icontains='mahalla')
        ).values_list('student_id', flat=True)

        paid_student_ids = MonthlyPayment.objects.filter(
            month=req_month
        ).exclude(
            Q(amount_paid__isnull=True) |
            Q(amount_paid__exact='') |
            Q(amount_paid__exact='0') |
            Q(status='unpaid')
        ).values_list('student_id', flat=True)

        students_qs = students_qs.exclude(is_local=True)\
                                 .exclude(id__in=mahalla_pay_ids)\
                                 .exclude(id__in=paid_student_ids)

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

    deleted_ids = DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)
    students_qs = Student.objects.filter(
        time_slot__group=group,
        is_active=True,
        left_date__isnull=True,
    ).exclude(
        id__in=deleted_ids
    ).select_related('time_slot')

    if search_q:
        words = search_q.split()
        q_filter = Q()
        for w in words:
            q_filter |= Q(first_name__icontains=w) | Q(last_name__icontains=w) | Q(phone_1__icontains=w) | Q(phone_2__icontains=w)
        students_qs = students_qs.filter(q_filter)

    # Qarzdorlar filtri: Mahalla (is_local=True), arxiv/ketganlar va to'lov qilganlar QuerySet'dan to'liq chiqarib tashlansin
    if status_filter == 'unpaid':
        mahalla_pay_ids = MonthlyPayment.objects.filter(
            month=req_month
        ).filter(
            Q(amount_paid__icontains='mahalla') | Q(notes__icontains='mahalla')
        ).values_list('student_id', flat=True)

        paid_student_ids = MonthlyPayment.objects.filter(
            month=req_month
        ).exclude(
            Q(amount_paid__isnull=True) |
            Q(amount_paid__exact='') |
            Q(amount_paid__exact='0') |
            Q(status='unpaid')
        ).values_list('student_id', flat=True)

        students_qs = students_qs.exclude(is_local=True)\
                                 .exclude(id__in=mahalla_pay_ids)\
                                 .exclude(id__in=paid_student_ids)

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
    Admin tomonidan to'lovni saqlash / yangilash.
    Matn ('mahalla', 'B.U' va h.k.) yozilganda:
    status = 'paid', qarz = 0 bo'ladi.
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
    payment_type = request.POST.get('payment_type', 'cash').strip()
    valid_types = [t[0] for t in MonthlyPayment.PAYMENT_TYPE_CHOICES]
    if payment_type not in valid_types:
        payment_type = 'cash'
    notes = request.POST.get('notes', '').strip()

    if not month:
        month = date.today().strftime('%Y-%m')

    if not student_id:
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
            return JsonResponse({'status': 'error', 'msg': "O'quvchi ko'rsatilmadi"}, status=400)
        messages.error(request, "O'quvchi tanlanmadi!")
        return redirect('admin_payments_all')

    student = get_object_or_404(Student, id=student_id)

    # Miqdorni aniqlash va matnli to'lovni tekshirish
    amount_raw_str = str(amount_raw).strip()
    clean_digits = amount_raw_str.replace(' ', '').replace("'", '').replace(',', '')
    has_text = _is_text_payment(amount_raw_str)
    is_mahalla_user = (
        getattr(student, 'is_local', False) or
        getattr(student, 'is_mahalla', False) or
        ('mahalla' in amount_raw_str.lower()) or
        ('mahalla' in notes.lower())
    )

    if clean_digits.isdigit() and int(clean_digits) > 0:
        amount_num = int(clean_digits)
        amount_to_save = str(amount_num)
        new_status = 'paid'
    elif is_mahalla_user:
        amount_num = 0
        amount_to_save = amount_raw_str if amount_raw_str else 'mahalla'
        new_status = 'paid'
    else:
        amount_num = 0
        amount_to_save = amount_raw_str
        # Matnli to'lov ('mahalla', 'B.U') bo'lsa -> 'paid'
        new_status = 'paid' if has_text else 'unpaid'

    p_date = date.today()

    # Kelgan sanani tahrirlash (joined_date)
    joined_date_raw = request.POST.get('joined_date', '').strip()
    if joined_date_raw:
        try:
            new_joined = datetime.strptime(joined_date_raw, '%Y-%m-%d').date()
            if new_joined > date.today():
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
                    return JsonResponse({'status': 'error', 'msg': "Kelgan sana kelajakdagi sana bo'lishi mumkin emas!"}, status=400)
                messages.error(request, "Kelgan sana kelajakdagi sana bo'lishi mumkin emas! Bugungi yoki o'tgan sanani kiriting.")
            elif student.joined_date != new_joined:
                student.joined_date = new_joined
                student.save(update_fields=['joined_date'])
        except Exception:
            pass

    mp, created = MonthlyPayment.objects.update_or_create(
        student=student,
        month=month,
        defaults={
            'amount_paid': amount_to_save,
            'payment_type': payment_type,
            'payment_date': p_date,
            'status': new_status,
            'notes': notes,
        }
    )

    try:
        AuditLog.objects.create(
            action_type='payment',
            actor=request.user if (hasattr(request, 'user') and request.user.is_authenticated) else None,
            actor_name="Direktor / Admin",
            title=f"To'lov qayd etildi: {student.first_name} {student.last_name} ({amount_to_save} - {mp.get_payment_type_display()})",
            details=f"Guruh: {student.time_slot.group.name if student.time_slot and student.time_slot.group else '-'}, Oy: {month}, Turi: {mp.get_payment_type_display()}, Holat: {new_status}"
        )
    except Exception:
        pass

    # AJAX javobi
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('ajax') == '1':
        rate_val = student.payment_rate or 0

        if mp.payment_date and (amount_num > 0 or has_text or is_mahalla_user):
            if relativedelta:
                due_date_obj = mp.payment_date + relativedelta(months=1)
            else:
                yr, mn = mp.payment_date.year, mp.payment_date.month
                mn += 1
                if mn > 12:
                    mn = 1
                    yr += 1
                max_next = calendar.monthrange(yr, mn)[1]
                due_date_obj = date(yr, mn, min(mp.payment_date.day, max_next))
            due_date_str = due_date_obj.strftime('%d.%m.%Y')
        else:
            due_date_str = ''

        is_paid = (new_status == 'paid') or has_text or is_mahalla_user
        if is_mahalla_user:
            status_display = "Mahalla" if amount_num == 0 else f"To'langan ({amount_to_save})"
            st_code_resp = 'paid'
            debt = 0
        elif has_text:
            status_display = f"To'langan ({amount_to_save})"
            st_code_resp = 'paid'
            debt = 0
        elif is_paid:
            status_display = "To'langan"
            st_code_resp = 'paid'
            debt = 0
        else:
            status_display = "Qarzdor"
            st_code_resp = 'unpaid'
            debt = rate_val

        return JsonResponse({
            'status': 'success',
            'payment_id': mp.id,
            'amount_paid': mp.amount_paid,
            'amount_paid_num': amount_num,
            'amount_paid_formatted': f"{amount_num:,}".replace(',', ' ') if amount_num > 0 else (amount_to_save if (has_text or is_mahalla_user) else '0'),
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
            'debt_formatted': f"{debt:,}".replace(',', ' ') if debt > 0 else '0',
        })

    # Oddiy redirect javob
    formatted_amount = f"{amount_num:,}".replace(',', ' ')
    type_display = mp.get_payment_type_display()
    if amount_num > 0:
        messages.success(
            request,
            f"✅ {student.first_name} {student.last_name}: {formatted_amount} so'm [{type_display}] "
            f"({month}) uchun muvaffaqiyatli saqlandi!"
        )
    elif is_mahalla_user:
        messages.success(
            request,
            f"✅ {student.first_name} {student.last_name}: Mahalla toifasi "
            f"({month}) uchun muvaffaqiyatli saqlandi!"
        )
    elif has_text:
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
    """Tanlangan to'lov yozuvini bekor qilish / o'chirish."""
    if not _admin_only(request):
        return redirect('home')
    mp = get_object_or_404(MonthlyPayment, id=id)
    if request.method == 'POST':
        student_name = f"{mp.student.first_name} {mp.student.last_name}"
        month = mp.month
        mp.delete()
        messages.success(request, f"{student_name}ning {month} oyi uchun to'lovi o'chirildi.")
    next_url = request.GET.get('next') or request.POST.get('next') or request.META.get('HTTP_REFERER') or 'admin_payments_all'
    return redirect(next_url)


@admin_required
def payment_mark(request):
    """Eski payment_mark endpointi bilan moslik (backward compatibility)."""
    return admin_payment_save(request)


@admin_required
def export_payments_excel(request):
    """To'lovlar jadvalini professional Excel fayl (.xlsx) shaklida eksport qilish."""
    if not _admin_only(request):
        return redirect('home')

    req_month = request.GET.get('month', date.today().strftime('%Y-%m'))
    group_id = request.GET.get('group_id', '')
    status_filter = request.GET.get('status', 'all')
    is_local_param = request.GET.get('is_local', '')

    if is_local_param in ('1', 'true', 'True') or status_filter == 'local':
        status_filter = 'local'

    group = None
    is_grp = bool(group_id and group_id.isdigit())
    deleted_ids = DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)

    if is_grp:
        group = get_object_or_404(Group.objects.select_related('teacher'), id=int(group_id))
        students_qs = Student.objects.filter(
            time_slot__group=group,
            is_active=True,
            left_date__isnull=True,
        ).exclude(
            id__in=deleted_ids
        ).select_related('time_slot')
        base_title = f"Guruh To'lovlari — {group.name}"
        safe_name = "".join(c for c in group.name if c.isalnum() or c in (' ', '_', '-')).strip().replace(' ', '_')
        base_filename = f"Toluvlar_{safe_name}"
        group_info = f"Fan: {group.subject_name} | O'qituvchi: {group.teacher or '—'}"
    else:
        students_qs = Student.objects.filter(
            is_active=True,
            left_date__isnull=True,
        ).exclude(
            id__in=deleted_ids
        ).select_related('time_slot__group__teacher', 'time_slot__group')
        base_title = "Barcha Guruhlar Umumiy To'lovlar Jadvali"
        base_filename = "Humo_Umumiy_Toluvlar"
        group_info = "Barcha guruhlar"

    # Qarzdorlar filtri: Mahalla (is_local=True), arxiv/ketganlar va to'lov qilganlar QuerySet'dan to'liq chiqarib tashlansin
    if status_filter == 'unpaid':
        mahalla_pay_ids = MonthlyPayment.objects.filter(
            month=req_month
        ).filter(
            Q(amount_paid__icontains='mahalla') | Q(notes__icontains='mahalla')
        ).values_list('student_id', flat=True)

        paid_student_ids = MonthlyPayment.objects.filter(
            month=req_month
        ).exclude(
            Q(amount_paid__isnull=True) |
            Q(amount_paid__exact='') |
            Q(amount_paid__exact='0') |
            Q(status='unpaid')
        ).values_list('student_id', flat=True)

        students_qs = students_qs.exclude(is_local=True)\
                                 .exclude(id__in=mahalla_pay_ids)\
                                 .exclude(id__in=paid_student_ids)

    if status_filter == 'paid':
        type_label = "To'liq To'laganlar"
        filename = f"{base_filename}_Tolaganlar_{req_month}.xlsx"
    elif status_filter == 'overdue':
        type_label = "Kechikkanlar (Muddati O'tganlar)"
        filename = f"{base_filename}_Kechikkanlar_{req_month}.xlsx"
    elif status_filter == 'unpaid':
        type_label = "Qarzdorlar (Umuman to'lamaganlar)"
        filename = f"{base_filename}_Qarzdorlar_{req_month}.xlsx"
    elif status_filter == 'local':
        type_label = "Mahalladan Kelganlar"
        filename = f"{base_filename}_Mahalladan_{req_month}.xlsx"
    else:
        type_label = "Barcha O'quvchilar"
        filename = f"{base_filename}_{req_month}.xlsx"

    title = f"{base_title} ({type_label})"
    subtitle = f"{group_info} | Holat: {type_label} | Davr: {req_month} | Yuklangan: {date.today().strftime('%d.%m.%Y')}"

    filter_for_data = 'all' if status_filter == 'yearly' else status_filter
    rows, stats = _build_payments_data(students_qs, req_month, status_filter=filter_for_data, is_group_specific=is_grp)

    excel_file = generate_payments_excel(rows, title=title, subtitle=subtitle, stats=stats)

    response = HttpResponse(
        excel_file.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ─────────────────────────────────────────────
# 7. EXCEL DIRECT EXPORT (ADMIN)
# ─────────────────────────────────────────────

@admin_required
def admin_export_excel(request):
    """Admin: Barcha o'qituvchilar va ularga biriktirilgan guruhlar/o'quvchilar ro'yxatini yuklab berish."""
    if not _admin_only(request):
        return redirect('home')

    curr_month = date.today().strftime('%Y-%m')
    payments = MonthlyPayment.objects.filter(month=curr_month)
    payment_map = {p.student_id: p.amount_paid for p in payments}

    wb = openpyxl.Workbook()

    ws_all = wb.active
    ws_all.title = "Barcha O'quvchilar"
    all_students = Student.objects.select_related('time_slot__group__teacher').all().order_by(
        'time_slot__group__teacher__first_name',
        'time_slot__group__teacher__last_name',
        'time_slot__group__name',
        'first_name',
        'last_name'
    )
    _populate_excel_sheet(ws_all, all_students, payment_map)

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
    """Guruh o'quvchilarini Excel fayl qilib yuklab berish."""
    if not _admin_only(request):
        return redirect('home')

    group = get_object_or_404(Group, id=group_id)
    curr_month = date.today().strftime('%Y-%m')
    payments = MonthlyPayment.objects.filter(month=curr_month)
    payment_map = {p.student_id: p.amount_paid for p in payments}

    students = Student.objects.select_related('time_slot__group__teacher').filter(
        time_slot__group=group
    ).order_by('first_name', 'last_name')

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{group.name[:28]}"
    _populate_excel_sheet(ws, students, payment_map)

    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="Guruh_{group.name}_{curr_month}.xlsx"'
    wb.save(response)
    return response


# ─────────────────────────────────────────────
# 8. SHARED: SANANI YANGILASH VA RASM YUKLASH
# ─────────────────────────────────────────────

def update_student_joined_date(request, id):
    """Admin yoki o'qituvchi tomonidan kelgan sanani yangilash (AJAX)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': "Noto'g'ri so'rov"}, status=400)

    is_admin = bool(_admin_only(request))
    is_teacher = request.user.is_authenticated and getattr(request.user, 'role', '') == 'teacher'

    if not is_admin and not is_teacher:
        return JsonResponse({'status': 'error', 'msg': 'Ruxsat berilmagan'}, status=403)

    student = get_object_or_404(Student, id=id)

    if is_teacher and not is_admin:
        if not (student.time_slot and student.time_slot.group and
                student.time_slot.group.teacher and
                student.time_slot.group.teacher.user == request.user):
            return JsonResponse({'status': 'error', 'msg': "Bu o'quvchi sizning guruhingizga tegishli emas"}, status=403)

    joined_date_raw = request.POST.get('joined_date', '').strip()
    if not joined_date_raw:
        return JsonResponse({'status': 'error', 'msg': "Kelgan sana kiritilmadi"}, status=400)

    try:
        new_date = datetime.strptime(joined_date_raw, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'status': 'error', 'msg': "Sana formati noto'g'ri (YYYY-MM-DD)"}, status=400)

    if new_date > date.today():
        return JsonResponse({
            'status': 'error',
            'msg': "Kelgan sana kelajakdagi sana bo'lishi mumkin emas! Bugungi yoki o'tgan sanani kiriting."
        }, status=400)

    student.joined_date = new_date
    student.save(update_fields=['joined_date'])

    return JsonResponse({
        'status': 'success',
        'student_id': student.id,
        'joined_date': new_date.strftime('%Y-%m-%d'),
        'display_date': new_date.strftime('%d.%m.%Y'),
        'msg': "Kelgan sana muvaffaqiyatli saqlandi!"
    })


def student_photo_upload(request, id):
    """O'quvchiga rasm yuklash / yangilash."""
    is_admin = bool(request.session.get('admin_authenticated'))
    is_teacher = request.user.is_authenticated and getattr(request.user, 'role', '') == 'teacher'

    if not is_admin and not is_teacher:
        return JsonResponse({'status': 'error', 'msg': 'Ruxsat berilmagan'}, status=403)

    student = get_object_or_404(Student, id=id)

    if is_teacher and not is_admin:
        if not (student.time_slot and student.time_slot.group and
                student.time_slot.group.teacher and
                student.time_slot.group.teacher.user == request.user):
            return JsonResponse({'status': 'error', 'msg': "Bu o'quvchi sizning guruhingizda emas"}, status=403)

    if request.method == 'POST':
        photo = request.FILES.get('photo')
        if not photo:
            return JsonResponse({'status': 'error', 'msg': 'Rasm fayli tanlanmadi'}, status=400)

        opt_photo, err = optimize_student_photo(photo)
        if not opt_photo:
            return JsonResponse({'status': 'error', 'msg': f"Rasm formatini qayta ishlab bo'lmadi: {err or 'Noma\'lum xatolik'}"}, status=400)

        if student.photo:
            try:
                if os.path.exists(student.photo.path):
                    os.remove(student.photo.path)
            except Exception:
                pass

        student.photo.save(opt_photo.name, opt_photo, save=True)
        return JsonResponse({'status': 'success', 'photo_url': student.photo.url})

    return JsonResponse({'status': 'error', 'msg': "Noto'g'ri so'rov usuli"}, status=405)


# ─────────────────────────────────────────────
# 9. ADMIN CHAT
# ─────────────────────────────────────────────

@admin_required
def admin_chat_list(request):
    admin_user = request.user if (request.user.is_authenticated and request.user.role == 'admin') else CustomUser.objects.filter(role='admin').first()
    teachers = Teacher.objects.all()
    teacher_data = []
    for t in teachers:
        unread = 0
        if admin_user:
            unread = Message.objects.filter(sender=t.user, receiver=admin_user, is_read=False).count()
        teacher_data.append({'teacher': t, 'unread': unread})
    return render(request, 'admin_chat_list.html', {'teacher_data': teacher_data})


@admin_required
def admin_chat_detail(request, teacher_id):
    admin_user = request.user if (request.user.is_authenticated and request.user.role == 'admin') else CustomUser.objects.filter(role='admin').first()
    teacher = get_object_or_404(Teacher, id=teacher_id)
    t_user = teacher.user

    if admin_user:
        Message.objects.filter(sender=t_user, receiver=admin_user, is_read=False).update(is_read=True)

    messages_qs = Message.objects.filter(
        (Q(sender=admin_user) & Q(receiver=t_user)) |
        (Q(sender=t_user) & Q(receiver=admin_user))
    ).order_by('timestamp') if admin_user else Message.objects.none()

    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        if content and admin_user:
            Message.objects.create(sender=admin_user, receiver=t_user, content=content)
            return redirect('admin_chat_detail', teacher_id=teacher_id)

    return render(request, 'admin_chat_detail.html', {
        'chat_user': t_user,
        'teacher': teacher,
        'chat_messages': messages_qs
    })


# ─────────────────────────────────────────────
# 10. MENAJERLAR VA XODIMLAR BOSHQARUVI
# ─────────────────────────────────────────────

@admin_only_required
def admin_manager_create(request):
    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        monthly_salary = request.POST.get('monthly_salary', '0').strip()
        late_fine_rate = request.POST.get('late_fine_rate', '2.0').strip()
        work_start_time = request.POST.get('work_start_time', '09:00').strip()
        work_end_time = request.POST.get('work_end_time', '18:00').strip()
        photo_file = request.FILES.get('photo')

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning!")
            return render(request, 'admin_manager_create.html', request.POST)

        login_str = f"m_{first_name.lower()[:3]}_{random.randint(100, 999)}"
        pwd_str = ''.join(random.choices(string.ascii_letters + string.digits, k=8))

        user = CustomUser.objects.create_user(username=login_str, password=pwd_str, role='manager')

        opt_photo = None
        if photo_file:
            opt_photo, err = optimize_student_photo(photo_file)

        settings_obj = FaceIDSettings.get_settings()
        salary_val = _safe_decimal(monthly_salary, '0') if is_admin_user(request) else Decimal('0')
        fine_rate_val = _safe_decimal(late_fine_rate, str(settings_obj.default_late_fine_rate)) if is_admin_user(request) else settings_obj.default_late_fine_rate
        Manager.objects.create(
            user=user,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            generated_login=login_str,
            generated_password=pwd_str,
            monthly_salary=salary_val,
            late_fine_rate=fine_rate_val,
            work_start_time=work_start_time or '09:00',
            work_end_time=work_end_time or '18:00',
            photo=opt_photo
        )
        messages.success(request, f"Menejer '{first_name} {last_name}' muvaffaqiyatli qo'shildi.")
        return redirect('admin_dashboard')

    settings_obj = FaceIDSettings.get_settings()
    return render(request, 'admin_manager_create.html', {
        'default_fine_rate': settings_obj.default_late_fine_rate,
        'default_start_time': settings_obj.default_work_start_time,
        'default_end_time': settings_obj.default_work_end_time,
    })


@admin_only_required
def admin_manager_edit(request, id):
    manager = get_object_or_404(Manager, id=id)

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        monthly_salary = request.POST.get('monthly_salary', '0').strip()
        late_fine_rate = request.POST.get('late_fine_rate', '2.0').strip()
        work_start_time = request.POST.get('work_start_time', '09:00').strip()
        work_end_time = request.POST.get('work_end_time', '18:00').strip()
        photo_file = request.FILES.get('photo')
        new_password = request.POST.get('new_password', '').strip()

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning!")
            return render(request, 'admin_manager_edit.html', {'manager': manager})

        manager.first_name = first_name
        manager.last_name = last_name
        manager.phone = phone
        if is_admin_user(request):
            manager.monthly_salary = _safe_decimal(monthly_salary, str(manager.monthly_salary))
            manager.late_fine_rate = _safe_decimal(late_fine_rate, str(manager.late_fine_rate))
        manager.work_start_time = work_start_time or '09:00'
        manager.work_end_time = work_end_time or '18:00'

        if photo_file:
            opt_photo, err = optimize_student_photo(photo_file)
            if opt_photo:
                manager.photo = opt_photo

        if new_password:
            manager.user.set_password(new_password)
            manager.user.save()
            manager.generated_password = new_password

        manager.save()
        messages.success(request, f"Menejer '{manager.first_name} {manager.last_name}' ma'lumotlari yangilandi.")
        return redirect('admin_dashboard')

    return render(request, 'admin_manager_edit.html', {'manager': manager})


@admin_only_required
def admin_manager_delete(request, id):
    manager = get_object_or_404(Manager, id=id)
    if request.method == 'POST':
        user = manager.user
        name = f"{manager.first_name} {manager.last_name}"
        manager.delete()
        if user:
            user.delete()
        messages.success(request, f"Menejer '{name}' tizimdan o'chirildi.")
        return redirect('admin_dashboard')
    return redirect('admin_dashboard')


@admin_only_required
def admin_teacher_edit(request, id):
    teacher = get_object_or_404(Teacher, id=id)

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        phone = request.POST.get('phone', '').strip()
        monthly_salary = request.POST.get('monthly_salary', '0').strip()
        late_fine_rate = request.POST.get('late_fine_rate', '2.0').strip()
        work_start_time = request.POST.get('work_start_time', '09:00').strip()
        work_end_time = request.POST.get('work_end_time', '18:00').strip()
        photo_file = request.FILES.get('photo')
        new_password = request.POST.get('new_password', '').strip()
        can_edit = request.POST.get('can_edit_students') == '1'

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning!")
            return render(request, 'admin_teacher_edit.html', {'teacher': teacher})

        teacher.first_name = first_name
        teacher.last_name = last_name
        teacher.phone = phone
        if is_admin_user(request):
            teacher.monthly_salary = _safe_decimal(monthly_salary, str(teacher.monthly_salary))
            teacher.late_fine_rate = _safe_decimal(late_fine_rate, str(teacher.late_fine_rate))
        teacher.work_start_time = work_start_time or '09:00'
        teacher.work_end_time = work_end_time or '18:00'
        teacher.can_edit_students = can_edit

        if photo_file:
            opt_photo, err = optimize_student_photo(photo_file)
            if opt_photo:
                teacher.photo = opt_photo

        if new_password:
            teacher.user.set_password(new_password)
            teacher.user.save()
            teacher.generated_password = new_password

        teacher.save()
        messages.success(request, f"O'qituvchi '{teacher.first_name} {teacher.last_name}' ma'lumotlari muvaffaqiyatli saqlandi.")
        return redirect('admin_dashboard')

    return render(request, 'admin_teacher_edit.html', {'teacher': teacher})


# ─────────────────────────────────────────────
# 11. FACE ID JURNALI VA SOZLAMALARI (Faqat Admin)
# ─────────────────────────────────────────────

@admin_only_required
def admin_face_id_logs(request):
    """Barcha xodimlarning Face ID davomat jurnali (kelish-ketish va kechikishlar).
    Faqat Markaz Rahbari (Admin) uchun ruxsat etilgan."""
    filter_period = request.GET.get('period', 'today')
    filter_date_str = request.GET.get('date', '')
    filter_role = request.GET.get('role', 'all')
    filter_status = request.GET.get('status', 'all')

    today = date.today()
    logs_qs = FaceIDLog.objects.select_related('user').all()

    if filter_date_str:
        try:
            filter_date = datetime.strptime(filter_date_str, '%Y-%m-%d').date()
            logs_qs = logs_qs.filter(date=filter_date)
            filter_period = 'custom'
        except Exception:
            filter_date_str = today.strftime('%Y-%m-%d')
            logs_qs = logs_qs.filter(date=today)
            filter_period = 'today'
    elif filter_period == 'today':
        logs_qs = logs_qs.filter(date=today)
        filter_date_str = today.strftime('%Y-%m-%d')
    elif filter_period == 'month':
        logs_qs = logs_qs.filter(date__year=today.year, date__month=today.month)
    elif filter_period == 'all':
        pass  # Barcha yozuvlar

    if filter_role == 'manager':
        logs_qs = logs_qs.filter(user__role='manager')
    elif filter_role == 'teacher':
        logs_qs = logs_qs.filter(user__role='teacher')

    if filter_status == 'on_time':
        logs_qs = logs_qs.filter(is_late=False)
    elif filter_status == 'late':
        logs_qs = logs_qs.filter(is_late=True)

    logs_list = list(logs_qs.order_by('-date', '-check_in_time'))

    # Statistika
    total_scans = len(logs_list)
    late_scans = sum(1 for l in logs_list if l.is_late)
    ontime_scans = total_scans - late_scans
    total_fines = sum(l.fine_amount_deducted for l in logs_list)

    # ── Barcha xodimlarning Face ID holati (Enrollment status) ──
    staff_status_list = []
    has_cv = False
    try:
        import core.face_service as fs
        has_cv = getattr(fs, 'HAS_OPENCV', False)
    except Exception:
        fs = None

    for m in Manager.objects.all().select_related('user'):
        has_photo = bool(m.photo)
        face_detected = False
        if has_photo:
            try:
                if os.path.exists(m.photo.path) and has_cv and fs:
                    cv_img = fs._load_cv_image_from_file_or_path(m.photo.path)
                    if cv_img is not None:
                        roi, _ = fs.extract_face_roi(cv_img)
                        face_detected = (roi is not None)
            except Exception:
                pass
        staff_status_list.append({
            'id': m.id,
            'role': 'manager',
            'role_title': 'Menejer',
            'full_name': f"{m.first_name} {m.last_name}".strip() or m.user.username,
            'username': m.user.username,
            'phone': m.phone or '—',
            'has_photo': has_photo,
            'face_detected': face_detected,
            'photo_url': m.photo.url if m.photo else '',
            'work_time': f"{m.work_start_time.strftime('%H:%M')} - {m.work_end_time.strftime('%H:%M')}" if m.work_start_time else '—',
            'is_active': m.is_active,
        })

    for t in Teacher.objects.all().select_related('user'):
        has_photo = bool(t.photo)
        face_detected = False
        if has_photo:
            try:
                if os.path.exists(t.photo.path) and has_cv and fs:
                    cv_img = fs._load_cv_image_from_file_or_path(t.photo.path)
                    if cv_img is not None:
                        roi, _ = fs.extract_face_roi(cv_img)
                        face_detected = (roi is not None)
            except Exception:
                pass
        staff_status_list.append({
            'id': t.id,
            'role': 'teacher',
            'role_title': "O'qituvchi",
            'full_name': f"{t.first_name} {t.last_name}".strip() or t.user.username,
            'username': t.user.username,
            'phone': t.phone or '—',
            'has_photo': has_photo,
            'face_detected': face_detected,
            'photo_url': t.photo.url if t.photo else '',
            'work_time': f"{t.work_start_time.strftime('%H:%M')} - {t.work_end_time.strftime('%H:%M')}" if t.work_start_time else '—',
            'is_active': t.is_active,
        })

    ready_staff_count = sum(1 for s in staff_status_list if s['face_detected'])
    missing_staff_count = len(staff_status_list) - ready_staff_count

    return render(request, 'admin_face_id_logs.html', {
        'logs': logs_list,
        'filter_period': filter_period,
        'filter_date': filter_date_str,
        'filter_role': filter_role,
        'filter_status': filter_status,
        'total_scans': total_scans,
        'late_scans': late_scans,
        'ontime_scans': ontime_scans,
        'total_fines': total_fines,
        'staff_status_list': staff_status_list,
        'ready_staff_count': ready_staff_count,
        'missing_staff_count': missing_staff_count,
    })


@admin_only_required
def admin_face_id_sync_staff(request):
    """Barcha mavjud xodimlarni Face ID ga bir marta bosish bilan sinxronlash."""
    settings_obj = FaceIDSettings.get_settings()
    synced_count = 0
    created_profiles = 0

    # Menejerlar tekshiruvi
    for u in CustomUser.objects.filter(role='manager'):
        if not hasattr(u, 'manager_profile'):
            Manager.objects.create(
                user=u,
                first_name=u.first_name or u.username,
                last_name=u.last_name or '',
                phone='',
                generated_login=u.username,
                generated_password='***',
                work_start_time=settings_obj.default_work_start_time,
                work_end_time=settings_obj.default_work_end_time,
                late_fine_rate=settings_obj.default_late_fine_rate
            )
            created_profiles += 1

    # O'qituvchilar tekshiruvi
    for u in CustomUser.objects.filter(role='teacher'):
        if not hasattr(u, 'teacher_profile'):
            Teacher.objects.create(
                user=u,
                first_name=u.first_name or u.username,
                last_name=u.last_name or '',
                phone='',
                generated_login=u.username,
                generated_password='***',
                work_start_time=settings_obj.default_work_start_time,
                work_end_time=settings_obj.default_work_end_time,
                late_fine_rate=settings_obj.default_late_fine_rate
            )
            created_profiles += 1

    for m in Manager.objects.all():
        if not m.work_start_time:
            m.work_start_time = settings_obj.default_work_start_time
        if not m.work_end_time:
            m.work_end_time = settings_obj.default_work_end_time
        m.save()
        synced_count += 1

    for t in Teacher.objects.all():
        if not t.work_start_time:
            t.work_start_time = settings_obj.default_work_start_time
        if not t.work_end_time:
            t.work_end_time = settings_obj.default_work_end_time
        t.save()
        synced_count += 1

    messages.success(
        request,
        f"Face ID sinxronizatsiyasi muvaffaqiyatli: {synced_count} ta xodim sozlamalari yangilandi, {created_profiles} ta yetishmayotgan profil yaratildi."
    )
    return redirect('admin_face_id_logs')


@admin_only_required
def admin_face_id_upload_photo(request, role, id):
    """Admin orqali xodimga Face ID yuz fotosuratini biriktirish (fayl yoki veb-kamera snapshot orqali)."""
    staff = None
    if role == 'teacher':
        staff = get_object_or_404(Teacher, id=id)
    elif role == 'manager':
        staff = get_object_or_404(Manager, id=id)
    else:
        return JsonResponse({'success': False, 'message': 'Noto\'g\'ri lavozim'}, status=400)

    photo_file = request.FILES.get('photo')
    image_base64 = request.POST.get('image_base64')

    if not photo_file and image_base64:
        try:
            if ',' in image_base64:
                header, image_base64 = image_base64.split(',', 1)
            decoded = base64.b64decode(image_base64)
            photo_file = ContentFile(decoded, name=f"face_{role}_{id}_{uuid.uuid4().hex[:6]}.jpg")
        except Exception as e:
            return JsonResponse({'success': False, 'message': f"Suratni o'qishda xatolik: {e}"}, status=400)

    if not photo_file:
        return JsonResponse({'success': False, 'message': "Hech qanday rasm tanlanmadi!"}, status=400)

    opt_photo, err = optimize_student_photo(photo_file)
    if not opt_photo:
        return JsonResponse({'success': False, 'message': f"Rasmni qayta ishlashda xatolik: {err}"}, status=400)

    staff.photo = opt_photo
    staff.save()

    # OpenCV biometrik tekshiruvi
    face_ok = False
    try:
        import core.face_service as fs
        if fs.HAS_OPENCV:
            cv_img = fs._load_cv_image_from_file_or_path(staff.photo.path)
            if cv_img is not None:
                roi, box = fs.extract_face_roi(cv_img)
                if roi is not None:
                    face_ok = True
    except Exception:
        pass

    msg = f"{staff.first_name} {staff.last_name} uchun Face ID yuz fotosurati muvaffaqiyatli biriktirildi!"
    if face_ok:
        msg += " (OpenCV biometrik tekshiruvdan o'tdi, yuz to'g'ri aniqlandi)."
    else:
        msg += " (Ogohlantirish: Rasm saqlandi, ammo yuz aniq aniqlanmadi, boshqa surat yuklash tavsiya etiladi)."

    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.POST.get('is_ajax'):
        return JsonResponse({
            'success': True,
            'message': msg,
            'photo_url': staff.photo.url if staff.photo else '',
            'face_detected': face_ok
        })

    messages.success(request, msg)
    return redirect('admin_face_id_logs')


@admin_only_required
def admin_face_id_settings(request):
    """
    Faqat Admin uchun Face ID sozlamalari paneli:
    - Markaz koordinatalari (lat, lon)
    - Ruxsat etilgan masofa chegarasi (Phone Volume Slider yordamida)
    - Kechikish jarima foizi (Phone Volume Slider yordamida)
    DIQQAT: Menejer uchun qat'iy taqiqlangan (403 Access Denied)!
    """
    settings_obj = FaceIDSettings.get_settings()

    if request.method == 'POST':
        lat_val = request.POST.get('latitude', '').strip()
        lon_val = request.POST.get('longitude', '').strip()
        radius_val = request.POST.get('allowed_radius_meters', '').strip()
        fine_rate_val = request.POST.get('default_late_fine_rate', '').strip()
        start_time_val = request.POST.get('default_work_start_time', '09:00').strip()
        end_time_val = request.POST.get('default_work_end_time', '18:00').strip()
        geofence_val = request.POST.get('is_geofencing_enabled') == 'on'

        try:
            if lat_val:
                settings_obj.latitude = float(lat_val.replace(',', '.'))
            if lon_val:
                settings_obj.longitude = float(lon_val.replace(',', '.'))
            if radius_val:
                settings_obj.allowed_radius_meters = int(float(radius_val.replace(',', '.')))
            if fine_rate_val:
                settings_obj.default_late_fine_rate = _safe_decimal(fine_rate_val, '2.0')
            settings_obj.default_work_start_time = start_time_val or '09:00'
            settings_obj.default_work_end_time = end_time_val or '18:00'
            settings_obj.is_geofencing_enabled = geofence_val
            settings_obj.save()
            messages.success(request, "Face ID va Geolokatsiya sozlamalari muvaffaqiyatli yangilandi! ✅")
            return redirect('admin_face_id_settings')
        except Exception as e:
            messages.error(request, f"Saqlashda xatolik yuz berdi: {e}")

    return render(request, 'admin_face_id_settings.html', {
        'settings': settings_obj,
    })


# ─────────────────────────────────────────────
# 12. OYLIK VA KECHIKISH JARIMALARI HISOBLASH
# ─────────────────────────────────────────────

@admin_only_required
def admin_salary_report(request):
    """
    Barcha xodimlarning oyligi, Face ID kechikish jarimalari va tasdiqlangan avanslarini avtomatik hisoblash sahifasi.
    DIQQAT: Faqat Markaz Rahbari / Admin ko'ra oladi (Menejer va O'qituvchi kira olmaydi)!
    """
    month_str = request.GET.get('month', date.today().strftime('%Y-%m'))
    try:
        year, month = map(int, month_str.split('-'))
    except Exception:
        year, month = date.today().year, date.today().month
        month_str = f"{year}-{month:02d}"

    # Barcha xodimlar
    all_teachers = Teacher.objects.filter(is_active=True).select_related('user')
    all_managers = Manager.objects.filter(is_active=True).select_related('user')

    reports = []
    total_base_payroll = Decimal(0)
    total_deductions = Decimal(0)
    total_advances_center = Decimal(0)
    total_net_payroll = Decimal(0)
    total_lates_center = 0
    total_bonuses_center = Decimal(0)

    for m in all_managers:
        summary = calculate_staff_salary_summary(m, year, month)
        summary['role_title'] = "Menejer"
        reports.append(summary)
        total_base_payroll += summary['base_salary']
        total_deductions += summary['total_fine_amount']
        total_advances_center += summary['total_advance_amount']
        total_bonuses_center += summary['total_bonus_amount']
        total_net_payroll += summary['net_salary']
        total_lates_center += summary['total_late_count']

    for t in all_teachers:
        summary = calculate_staff_salary_summary(t, year, month)
        summary['role_title'] = "O'qituvchi / Tarbiyachi"
        reports.append(summary)
        total_base_payroll += summary['base_salary']
        total_deductions += summary['total_fine_amount']
        total_advances_center += summary['total_advance_amount']
        total_bonuses_center += summary['total_bonus_amount']
        total_net_payroll += summary['net_salary']
        total_lates_center += summary['total_late_count']

    return render(request, 'admin_salary_report.html', {
        'reports': reports,
        'current_month': month_str,
        'year': year,
        'month': month,
        'total_base_payroll': total_base_payroll,
        'total_deductions': total_deductions,
        'total_advances_center': total_advances_center,
        'total_bonuses_center': total_bonuses_center,
        'total_net_payroll': total_net_payroll,
        'total_lates_center': total_lates_center,
    })


@admin_only_required
def admin_bonus_save(request):
    """Direktor tomonidan xodimga bonus qo'shish (Faqat Admin)."""
    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        month = request.POST.get('month', date.today().strftime('%Y-%m'))
        amount_raw = request.POST.get('amount', '0').strip().replace(',', '').replace(' ', '')
        reason = request.POST.get('reason', "Erta kelganlik / Faollik uchun bonus").strip()

        try:
            target_user = CustomUser.objects.get(id=user_id)
            amount = Decimal(amount_raw)
            if amount > 0:
                bonus = StaffBonus.objects.create(
                    user=target_user,
                    month=month,
                    date=date.today(),
                    amount=amount,
                    reason=reason,
                    created_by=request.user if (hasattr(request, 'user') and request.user.is_authenticated) else None
                )
                # Tizim Tarixiga yozish
                AuditLog.objects.create(
                    action_type='bonus',
                    actor=request.user if (hasattr(request, 'user') and request.user.is_authenticated) else None,
                    actor_name="Direktor",
                    title=f"Bonus berildi: {target_user.staff_full_name} (+{amount:,.0f} so'm)",
                    details=f"Sabab: {reason}. Oy: {month}."
                )
                messages.success(request, f"{target_user.staff_full_name} ga {amount:,.0f} so'm bonus muvaffaqiyatli qo'shildi! 🎉")
            else:
                messages.error(request, "Bonus summasi 0 dan katta bo'lishi kerak!")
        except Exception as e:
            messages.error(request, f"Bonus qo'shishda xatolik: {e}")

    return redirect(f"{request.META.get('HTTP_REFERER', '/admin/salary/report/')}?month={request.POST.get('month', '')}")



@admin_only_required
def admin_bonus_delete(request, id):
    """Berilgan bonusni bekor qilish (Faqat Admin)."""
    bonus = get_object_or_404(StaffBonus, id=id)
    u_name = bonus.user.staff_full_name
    amt = bonus.amount
    m = bonus.month
    bonus.delete()

    AuditLog.objects.create(
        action_type='bonus',
        actor=request.user if (hasattr(request, 'user') and request.user.is_authenticated) else None,
        actor_name="Direktor",
        title=f"Bonus bekor qilindi: {u_name} (-{amt:,.0f} so'm)",
        details=f"Oy: {m}."
    )
    messages.info(request, f"{u_name} ning {amt:,.0f} so'm bonusi bekor qilindi.")
    return redirect(request.META.get('HTTP_REFERER', '/admin/salary/report/'))


@admin_only_required
def admin_audit_logs(request):
    """
    Tizim Tarixi (Audit Log / History):
    Faqat Markaz Rahbari (Admin) uchun ruxsat etilgan.
    """
    filter_action = request.GET.get('action', 'all')
    filter_date = request.GET.get('date', '')
    search_q = request.GET.get('q', '').strip()

    logs_qs = AuditLog.objects.select_related('actor').all()

    if filter_action != 'all' and filter_action:
        logs_qs = logs_qs.filter(action_type=filter_action)

    if filter_date:
        try:
            d = datetime.strptime(filter_date, '%Y-%m-%d').date()
            logs_qs = logs_qs.filter(created_at__date=d)
        except Exception:
            pass

    if search_q:
        logs_qs = logs_qs.filter(
            Q(title__icontains=search_q) |
            Q(details__icontains=search_q) |
            Q(actor_name__icontains=search_q)
        )

    logs = logs_qs[:150]

    return render(request, 'admin_audit_logs.html', {
        'logs': logs,
        'filter_action': filter_action,
        'filter_date': filter_date,
        'search_q': search_q,
    })


@admin_only_required
def admin_salary_export_excel(request):
    """Oylik va jarimalar hisobini Excel (.xlsx) faylga eksport qilish (Faqat Admin)."""

    month_str = request.GET.get('month', date.today().strftime('%Y-%m'))
    try:
        year, month = map(int, month_str.split('-'))
    except Exception:
        year, month = date.today().year, date.today().month
        month_str = f"{year}-{month:02d}"

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Oylik_{month_str}"

    headers = [
        "№", "F.I.Sh.", "Lavozim", "Telefon", "Asosiy oylik (so'm)",
        "Ish kunlari", "Kechikishlar soni", "Kechikilgan daqiqa",
        "Jarima foizi (%)", "Jarima summasi (so'm)", "Avans (so'm)", "Bonus (so'm)", "Qo'lga tegadigan oylik (so'm)"
    ]
    ws.append(headers)

    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = openpyxl.styles.Font(bold=True, color="FFFFFF", size=11)
        cell.fill = openpyxl.styles.PatternFill(start_color="1E1B4B", end_color="1E1B4B", fill_type="solid")
        cell.alignment = openpyxl.styles.Alignment(horizontal="center", vertical="center")

    staff_list = list(Manager.objects.filter(is_active=True)) + list(Teacher.objects.filter(is_active=True))

    for idx, s in enumerate(staff_list, 1):
        summary = calculate_staff_salary_summary(s, year, month)
        ws.append([
            idx,
            f"{s.first_name} {s.last_name}",
            s.role_title,
            s.phone or "-",
            float(summary['base_salary']),
            summary['total_days'],
            summary['total_late_count'],
            summary['total_late_minutes'],
            float(summary['total_fine_percent']),
            float(summary['total_fine_amount']),
            float(summary['total_advance_amount']),
            float(summary['total_bonus_amount']),
            float(summary['net_salary']),
        ])


    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = f'attachment; filename="HUMO_Oylik_va_Jarimalar_{month_str}.xlsx"'
    wb.save(response)
    return response


# ─────────────────────────────────────────────
# 13. FACE ID TERMINAL & API
# ─────────────────────────────────────────────

@csrf_exempt
def face_id_terminal(request):
    """Markaz binosida planshet yoki kompyuter orqali xodimlar yuzini skanerlovchi terminal."""
    settings_obj = FaceIDSettings.get_settings()
    teachers = Teacher.objects.filter(is_active=True).select_related('user')
    managers = Manager.objects.filter(is_active=True).select_related('user')

    return render(request, 'face_id_terminal.html', {
        'settings': settings_obj,
        'teachers': teachers,
        'managers': managers,
    })


@csrf_exempt
def face_id_check_in(request):
    """Mobil qurilmalardan xodimlar o'z telefonlarida Face ID orqali davomat qilish sahifasi."""
    settings_obj = FaceIDSettings.get_settings()
    return render(request, 'face_id_mobile_checkin.html', {
        'settings': settings_obj,
    })


@csrf_exempt
def face_id_verify_api(request):
    """
    Hands-Free Face ID AI avtomatik skanerlash REST API:
    - Yuqori aniqlikdagi Deep Learning (YuNet & SFace) orqali kadrni tezkor tekshirish
    - Kelish va Ketishni aqlli farqlash
    - Non-blocking geolokatsiya (Kiosk rejimida GPS kechikishi kamerani to'xtatmaydi)
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': "Faqat POST so'rovi qabul qilinadi"}, status=405)

    img_data = request.POST.get('image')
    file_obj = request.FILES.get('photo')
    user_id = request.POST.get('user_id')
    lat = request.POST.get('latitude')
    lon = request.POST.get('longitude')
    is_kiosk = request.POST.get('is_kiosk') == '1'
    bypass_geo = (request.POST.get('bypass_geo') == '1') or (is_kiosk and (lat in (None, '', 'null', 'undefined') or lon in (None, '', 'null', 'undefined')))

    # Rasm obyekti
    scan_file = None
    if file_obj:
        scan_file = file_obj
    elif img_data:
        try:
            if ',' in img_data:
                header, img_data = img_data.split(',', 1)
            decoded = base64.b64decode(img_data)
            scan_file = ContentFile(decoded, name=f"scan_{uuid.uuid4().hex[:8]}.jpg")
        except Exception as e:
            return JsonResponse({'success': False, 'message': f"Tasvirni o'qishda xatolik: {e}"}, status=400)

    if not scan_file:
        return JsonResponse({'success': False, 'message': "Tasvir topilmadi! Iltimos qaytadan skanerlang."}, status=400)

    target_user = None
    similarity_score = 0.0

    if user_id:
        try:
            target_user = CustomUser.objects.get(id=user_id)
        except CustomUser.DoesNotExist:
            return JsonResponse({'success': False, 'message': "Tanlangan xodim topilmadi."}, status=404)
    else:
        # Avtomatik AI yuz qidirish (barcha faol menejer va o'qituvchilar 128-D embedding bo'yicha)
        match = match_staff_by_face(scan_file)
        if match:
            role, staff, score, dist = match
            target_user = staff.user
            similarity_score = score

    # Agar match topilmasa, lekin tizimga kirgan foydalanuvchi xodim (menejer, o'qituvchi, admin) bo'lsa
    if not target_user and request.user.is_authenticated and getattr(request.user, 'role', '') in ('manager', 'teacher', 'admin'):
        staff_obj = getattr(request.user, 'manager_profile', None) or getattr(request.user, 'teacher_profile', None)
        if staff_obj and staff_obj.photo and os.path.exists(staff_obj.photo.path):
            score, is_match, dist = compare_two_faces(staff_obj.photo.path, scan_file)
            if is_match and score >= 60.0:
                target_user = request.user
                similarity_score = score

    if not target_user:
        return JsonResponse({
            'success': False,
            'message': "Yuz aniqlanmadi yoki xodim topilmadi! Tizimda ushbu yuzga mos keluvchi xodim topilmadi."
        }, status=400)

    try:
        res = process_face_check_in(
            user=target_user,
            scan_file=scan_file,
            lat=lat,
            lon=lon,
            bypass_geo=bypass_geo
        )
        if similarity_score > 0 and 'match_score' not in res:
            res['match_score'] = similarity_score

        # Profil fotosurati URL ni qo'shish
        staff_obj = getattr(target_user, 'manager_profile', None) or getattr(target_user, 'teacher_profile', None)
        if staff_obj and staff_obj.photo:
            try:
                res['photo_url'] = staff_obj.photo.url
            except Exception:
                pass

        return JsonResponse(res)
    except Exception as e:
        print(f"process_face_check_in error: {e}")
        return JsonResponse({
            'success': False,
            'message': f"Davomatni qayd etishda tizim xatoligi yuz berdi: {str(e)}"
        }, status=500)


# ─────────────────────────────────────────────
# 14. MENEJER PANELI VA SHAXSIY JURNAL
# ─────────────────────────────────────────────

@login_required(login_url='/manager/login/')
def manager_dashboard(request):
    """
    Menejer Paneli:
    - Shaxsiy profil va Face ID jurnali
    - Oylik maosh va kechikish jarimalari ko'rinishi
    - O'quv markazi guruhlari va o'quvchilari
    - DIQQAT: Face ID Boshqaruvi va tizim sozlamalari MUTLAQO KO'RINMAYDI!
    """
    if request.user.role != 'manager':
        return redirect('home')

    manager = getattr(request.user, 'manager_profile', None)
    if not manager:
        manager, _ = Manager.objects.get_or_create(
            user=request.user,
            defaults={
                'first_name': request.user.first_name or request.user.username,
                'last_name': request.user.last_name or '',
                'generated_login': request.user.username,
                'generated_password': '***'
            }
        )
    today = date.today()

    # Bugungi kelish holati
    today_log = FaceIDLog.objects.filter(user=request.user, date=today).first()

    # Oylik hisob-kitob
    salary_summary = calculate_staff_salary_summary(manager, today.year, today.month)

    # Shaxsiy jurnallar
    face_logs = FaceIDLog.objects.filter(user=request.user).order_by('-date')[:30]

    # Guruhlar ro'yxati (menejer o'quv jarayonini ko'rishi uchun)
    groups = Group.objects.select_related('teacher').prefetch_related('time_slots__students')

    return render(request, 'manager_dashboard.html', {
        'manager': manager,
        'today_log': today_log,
        'salary_summary': salary_summary,
        'face_logs': face_logs,
        'groups': groups,
    })


@login_required
def staff_my_face_logs(request):
    """Har bir xodim (Menejer yoki O'qituvchi) uchun o'z shaxsiy Face ID kelib-ketish tarixi va avans so'rovlari."""
    user = request.user
    staff = user.staff_profile

    if not staff:
        messages.error(request, "Xodim profili biriktirilmagan.")
        return redirect('home')

    month_str = request.GET.get('month', date.today().strftime('%Y-%m'))
    try:
        year, month = map(int, month_str.split('-'))
    except Exception:
        year, month = date.today().year, date.today().month
        month_str = f"{year}-{month:02d}"

    salary_summary = calculate_staff_salary_summary(staff, year, month)
    my_advances = AdvanceRequest.objects.filter(user=user).order_by('-created_at')[:20]

    return render(request, 'staff_my_face_logs.html', {
        'staff': staff,
        'salary_summary': salary_summary,
        'current_month': month_str,
        'my_advances': my_advances,
    })


# ─────────────────────────────────────────────
# 15. AVANS SO'ROVLARI BOSHQARUVI (Advance Requests)
# ─────────────────────────────────────────────

@admin_required
def admin_advance_list(request):
    """
    Xodimlar tomonidan yuborilgan avans so'rovlari ro'yxati (Admin va Menejer uchun):
    - Ko'rish, holat bo'yicha saralash (Kutilmoqda, Tasdiqlandi, Rad etildi)
    - Tasdiqlash / Rad etish
    """
    filter_status = request.GET.get('status', 'all')
    filter_month = request.GET.get('month', '')

    advances_qs = AdvanceRequest.objects.select_related('user', 'reviewed_by').all()

    if filter_status != 'all' and filter_status:
        advances_qs = advances_qs.filter(status=filter_status)

    if filter_month:
        advances_qs = advances_qs.filter(month=filter_month)

    all_advances = AdvanceRequest.objects.all()
    stats = {
        'total': all_advances.count(),
        'pending': all_advances.filter(status='pending').count(),
        'approved': all_advances.filter(status='approved').count(),
        'rejected': all_advances.filter(status='rejected').count(),
        'approved_sum': sum((a.amount for a in all_advances.filter(status='approved')), Decimal(0)),
    }

    return render(request, 'admin_advances.html', {
        'advances': advances_qs,
        'filter_status': filter_status,
        'filter_month': filter_month,
        'stats': stats,
    })


@admin_required
def admin_advance_action(request, id):
    """Avans so'rovini tasdiqlash yoki rad etish (POST)."""
    if request.method != 'POST':
        return redirect('admin_advance_list')

    advance = get_object_or_404(AdvanceRequest, id=id)
    action = request.POST.get('action')  # 'approve' or 'reject'
    comment = request.POST.get('admin_comment', '').strip()

    reviewer_user = request.user if getattr(request, 'user', None) and request.user.is_authenticated else None
    actor_name = "Admin" if is_admin_user(request) else "Menejer"

    if action == 'approve':
        advance.status = 'approved'
        status_text = "tasdiqlandi ✅"
    elif action == 'reject':
        advance.status = 'rejected'
        status_text = "rad etildi ❌"
    else:
        messages.error(request, "Noto'g'ri buyruq yuborildi.")
        return redirect('admin_advance_list')

    advance.admin_comment = comment
    advance.reviewed_by = reviewer_user
    advance.reviewed_at = timezone.now()
    advance.save()

    AuditLog.objects.create(
        action_type='advance',
        actor=reviewer_user,
        actor_name=actor_name,
        title=f"Avans {status_text}: {advance.user.staff_full_name} ({advance.amount:,.0f} so'm)",
        details=f"Oy: {advance.month}. Izoh: {comment or 'Izoh kiritilmagan'}."
    )

    messages.success(request, f"{advance.user.staff_full_name} ning {advance.amount:,.0f} so'm miqdoridagi avansi {status_text}!")
    return redirect('admin_advance_list')


@login_required
def staff_advance_request(request):
    """Xodim (O'qituvchi yoki Menejer) shaxsiy kabinetidan avans so'rash."""
    if request.method == 'POST':
        amount_raw = request.POST.get('amount', '0').strip().replace(',', '').replace(' ', '')
        reason = request.POST.get('reason', '').strip()
        month = request.POST.get('month', date.today().strftime('%Y-%m')).strip()

        try:
            amount = Decimal(amount_raw)
            if amount <= 0:
                messages.error(request, "Avans summasi 0 dan katta bo'lishi kerak!")
                return redirect(request.META.get('HTTP_REFERER', '/staff/my-logs/'))

            advance = AdvanceRequest.objects.create(
                user=request.user,
                amount=amount,
                reason=reason,
                month=month,
                status='pending'
            )

            AuditLog.objects.create(
                action_type='advance',
                actor=request.user,
                actor_name=request.user.staff_full_name,
                title=f"Avans so'rovi yuborildi: {request.user.staff_full_name} ({amount:,.0f} so'm)",
                details=f"Oy: {month}. Sabab: {reason or 'Izoh kiritilmagan'}."
            )

            messages.success(request, f"{amount:,.0f} so'm miqdoridagi avans so'rovi adminga muvaffaqiyatli yuborildi! Tasdiqlangach oylikdan avtomatik chegiriladi.")
        except Exception as e:
            messages.error(request, f"Avans so'rovida xatolik: {e}")

    return redirect(request.META.get('HTTP_REFERER', '/staff/my-logs/'))


