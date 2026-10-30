"""
HUMO O'quv markazi — Student, Group, Attendance & Chat Views (student_views.py)
Guruhlar, dars vaqtlari, davomat jurnali, o'quvchilar, arxiv, vaqtinchalik o'quvchilar va chat.
"""
import os
from datetime import date, datetime

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .auth_views import _admin_only, admin_required, is_admin_or_manager
from .face_service import sync_staff_face_embedding
from .forms import TemporaryStudentForm
from .models import (
    Attendance, CustomUser, DeletedStudent, FaceIDLog, Group,
    Manager, Message, Student, Teacher, TemporaryStudent, TimeSlot,
)
from .payment_views import (
    _is_ajax, get_active_students_qs, heal_student_group_links, normalize_month_str,
)
from .schedule_helper import parse_schedule_weekdays
from .services import optimize_student_photo
from .teacher_views import _make_days_data, _student_visible_in_month
from .validators import contains_cyrillic


# ─────────────────────────────────────────────────────────────
# 1. YORDAMCHI FUNKSIYALAR (HELPERS)
# ─────────────────────────────────────────────────────────────

def _archive_student_record(student: Student, reason: str, group_name: str | None = None, clear_timeslot: bool = True):
    """O'quvchini DeletedStudent arxiv jadvaliga yozib, nofaol holatga o'tkazadi."""
    grp_name = group_name or (
        student.time_slot.group.name if (student.time_slot and student.time_slot.group) else "Noma'lum"
    )
    DeletedStudent.objects.create(
        student_name=f"{student.first_name} {student.last_name}".strip(),
        phones=f"{student.phone_1}, {student.phone_2 or ''}".rstrip(', '),
        group_name=grp_name,
        reason=reason,
        first_name=student.first_name,
        last_name=student.last_name,
        age=student.age,
        phone_1=student.phone_1,
        phone_2=student.phone_2,
        time_slot_id=student.time_slot.id if (clear_timeslot and student.time_slot) else None,
        student_id=student.id,
    )
    student.is_active = False
    student.left_date = date.today()
    if clear_timeslot:
        student.time_slot = None
    student.save()


def _parse_past_or_today_date(raw_str: str):
    """YYYY-MM-DD sanani o'qiydi. Agar kelajakdagi sana bo'lsa ValueError('future') qaytaradi."""
    parsed = datetime.strptime(raw_str, '%Y-%m-%d').date()
    if parsed > date.today():
        raise ValueError('future')
    return parsed


# ─────────────────────────────────────────────────────────────
# 2. ADMIN DASHBOARD
# ─────────────────────────────────────────────────────────────

@admin_required
def admin_dashboard(request):
    if not _admin_only(request):
        return redirect('home')

    heal_student_group_links()
    query = request.GET.get('q', '').strip()
    search_results = []
    if query:
        q_filter = Q()
        for w in query.split():
            q_filter |= Q(first_name__icontains=w) | Q(last_name__icontains=w)
        search_results = (
            get_active_students_qs()
            .filter(q_filter)
            .prefetch_related('monthly_payments')
            .order_by('first_name', 'last_name')
        )

    groups = Group.objects.select_related('teacher').prefetch_related('time_slots__students')
    teachers = Teacher.objects.filter(is_active=True).prefetch_related('groups')
    managers = Manager.objects.filter(is_active=True)

    # Bitta SQL aggregate so'rov orqali bugungi Face ID statistikasini olish
    agg = FaceIDLog.objects.filter(date=date.today()).aggregate(
        total_scans=Count('id'),
        on_time_count=Count('id', filter=Q(is_late=False)),
        late_count=Count('id', filter=Q(is_late=True)),
        total_fines=Sum('fine_amount_deducted'),
    )
    face_stats = {
        'total_scans': agg['total_scans'] or 0,
        'on_time_count': agg['on_time_count'] or 0,
        'late_count': agg['late_count'] or 0,
        'total_fines': agg['total_fines'] or 0,
    }

    return render(request, 'admin_dashboard.html', {
        'groups': groups,
        'teachers': teachers,
        'managers': managers,
        'face_stats': face_stats,
        'search_query': query,
        'search_results': search_results,
    })


# ─────────────────────────────────────────────────────────────
# 3. GURUHLAR VA DARS VAQTLARI
# ─────────────────────────────────────────────────────────────

@admin_required
def admin_group_create(request):
    if not _admin_only(request):
        return redirect('home')
    teachers = Teacher.objects.all()
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        teacher_id = request.POST.get('teacher_id')
        if contains_cyrillic(name):
            messages.error(request, "Guruh nomida faqat lotin alifbosidagi harflardan foydalaning (Kirill harflari taqiqlangan)!")
            return render(request, 'admin_group_create.html', {'teachers': teachers, 'name_val': name})

        teacher = get_object_or_404(Teacher, id=teacher_id) if teacher_id else None
        subject = request.POST.get('subject', '').strip() or name
        Group.objects.create(name=name, subject=subject, teacher=teacher)
        messages.success(request, f"'{name}' guruhi muvaffaqiyatli yaratildi.")
        return redirect('admin_dashboard')
    return render(request, 'admin_group_create.html', {'teachers': teachers})


@admin_required
def admin_group_delete(request, id):
    if not _admin_only(request):
        return redirect('home')
    group = get_object_or_404(Group, id=id)
    if request.method == 'POST':
        reason = f"Guruh o'chirilishi sababli arxivlandi ({group.name})"
        for ts in group.time_slots.prefetch_related('students'):
            for student in ts.students.all():
                _archive_student_record(student, reason=reason, group_name=group.name, clear_timeslot=False)
        group.delete()
        messages.success(request, f"'{group.name}' guruhi o'chirildi va o'quvchilar arxivlandi.")
    return redirect('admin_dashboard')


@admin_required
def admin_timeslot_delete(request, id):
    if not _admin_only(request):
        return redirect('home')
    timeslot = get_object_or_404(TimeSlot, id=id)
    group_id = timeslot.group_id
    if request.method == 'POST':
        # Agar guruhda boshqa dars vaqti (TimeSlot) mavjud bo'lsa,
        # o'quvchilar guruhdan uzilib qolmasligi uchun ularni o'sha TimeSlot'ga ko'chiramiz.
        fallback_ts = TimeSlot.objects.filter(group_id=group_id).exclude(id=timeslot.id).first()
        if fallback_ts:
            timeslot.students.update(time_slot=fallback_ts)
        timeslot.delete()
        messages.success(request, "Dars vaqti o'chirildi.")
    return redirect('admin_group_detail', id=group_id)


# ─────────────────────────────────────────────────────────────
# 4. GURUH JURNALI VA DAVOMAT
# ─────────────────────────────────────────────────────────────

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
            TimeSlot.objects.create(group=group, days=days, start_time=start_time, end_time=end_time)
            messages.success(request, "Yangi dars vaqti muvaffaqiyatli qo'shildi.")
        return redirect('admin_group_detail', id=group.id)

    heal_student_group_links()
    req_month, y, m = normalize_month_str(request.GET.get('month'))
    days_data, num_days = _make_days_data(y, m)
    timeslots = list(group.time_slots.prefetch_related('students'))
    deleted_ids = set(
        DeletedStudent.objects.filter(student_id__isnull=False).values_list('student_id', flat=True)
    )

    student_ids = [
        s.id for ts in timeslots for s in ts.students.all()
        if s.id not in deleted_ids and _student_visible_in_month(s, y, m)
    ]

    attendance_map = {sid: {} for sid in student_ids}
    if student_ids:
        attendances = Attendance.objects.filter(
            student_id__in=student_ids,
            date__range=(date(y, m, 1), date(y, m, num_days)),
        ).values_list('student_id', 'date', 'status')
        for sid, att_date, status in attendances:
            attendance_map[sid][att_date.day] = status

    for ts in timeslots:
        ts_weekdays = parse_schedule_weekdays(ts.days)
        ts.active_students = [
            s for s in ts.students.all()
            if s.id not in deleted_ids and _student_visible_in_month(s, y, m)
        ]
        for s in ts.active_students:
            s_att = attendance_map.get(s.id, {})
            s.attendance_matrix = [
                {
                    'day': d['day'],
                    'date_str': d['date_str'],
                    'is_lesson': d['weekday_idx'] in ts_weekdays,
                    'status': s_att.get(d['day'], ''),
                    'is_today': d['is_today'],
                    'is_past': d['is_past'],
                    'is_future': d['is_future'],
                    'can_mark': (d['weekday_idx'] in ts_weekdays) and d['is_today'],
                }
                for d in days_data
            ]

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
    if req_date_raw:
        try:
            req_date = datetime.strptime(req_date_raw, '%Y-%m-%d').date()
        except Exception:
            return JsonResponse({'status': 'error', 'msg': "Noto'g'ri sana formati!"}, status=400)
    else:
        req_date = today

    if req_date != today:
        return JsonResponse({'status': 'error', 'msg': "Yo'qlama faqat joriy kun (bugun) uchun kiritilishi mumkin!"}, status=400)

    status_Aliases = {'green': 'present', 'blue': 'present', 'red': 'absent', 'yellow': 'absent'}
    status_val = status_Aliases.get(status_val, status_val)
    student = get_object_or_404(Student, id=student_id)

    if status_val in ('present', 'absent'):
        try:
            Attendance.objects.update_or_create(
                student=student, date=req_date, defaults={'status': status_val}
            )
        except ValidationError as ve:
            err_msg = ve.message_dict.get('date', [str(ve)])[0] if hasattr(ve, 'message_dict') else str(ve)
            return JsonResponse({'status': 'error', 'msg': err_msg}, status=400)
        return JsonResponse({'status': 'success', 'attendance': status_val})

    Attendance.objects.filter(student=student, date=req_date).delete()
    return JsonResponse({'status': 'success', 'attendance': ''})


# ─────────────────────────────────────────────────────────────
# 5. O'QUVCHILAR VA ARXIV BOSHQARUVI
# ─────────────────────────────────────────────────────────────

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
        ctx = {
            'timeslot': timeslot,
            'first_name_val': first_name,
            'last_name_val': last_name,
            'age_val': age,
            'phone_1_val': phone_1,
            'phone_2_val': phone_2,
        }

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning (Kirill harflari taqiqlangan)!")
            return render(request, 'admin_student_create.html', ctx)

        if first_name and last_name and phone_1:
            student = Student(
                first_name=first_name,
                last_name=last_name,
                phone_1=phone_1,
                phone_2=phone_2,
                time_slot=timeslot,
                is_local=request.POST.get('is_local') in ('1', 'on', 'true', True),
                age=int(age) if age.isdigit() else None,
            )
            joined_date_raw = request.POST.get('joined_date', '').strip()
            if joined_date_raw:
                try:
                    student.joined_date = _parse_past_or_today_date(joined_date_raw)
                except ValueError as err:
                    if str(err) == 'future':
                        messages.error(request, "Kelgan sana kelajakdagi sana bo'lishi mumkin emas! Bugungi yoki o'tgan sanani kiriting.")
                        return render(request, 'admin_student_create.html', ctx)

            if photo:
                opt_photo, _ = optimize_student_photo(photo)
                student.photo = opt_photo or photo
            student.save()
            if student.photo:
                sync_staff_face_embedding(student)

            messages.success(request, f"O'quvchi '{first_name} {last_name}' muvaffaqiyatli qo'shildi.")
            return redirect('admin_group_detail', id=timeslot.group_id)
    return render(request, 'admin_student_create.html', {'timeslot': timeslot})


@admin_required
def admin_student_delete(request, id):
    """O'quvchini guruhdan o'chirish va Arxivga o'tkazish (Admin va Menejer uchun)."""
    if not (is_admin_or_manager(request) or _admin_only(request)):
        if _is_ajax(request):
            return JsonResponse({'status': 'error', 'msg': "Ruxsat etilmagan amal!"}, status=403)
        return redirect('home')

    student = get_object_or_404(Student, id=id)
    if request.method == 'POST':
        reason = request.POST.get('reason', '').strip()
        if contains_cyrillic(reason):
            messages.warning(request, "O'chirish sababida faqat lotin harflaridan foydalaning.")
            reason = "Admin tomonidan arxivlandi"
        elif not reason:
            reason = "Guruhdan chiqarildi"

        group_id = student.time_slot.group_id if (student.time_slot and student.time_slot.group) else None
        st_full_name = f"{student.first_name} {student.last_name}".strip()
        _archive_student_record(student, reason=reason, clear_timeslot=True)

        if _is_ajax(request):
            return JsonResponse({
                'status': 'success',
                'msg': f"O'quvchi '{st_full_name}' arxivga muvaffaqiyatli o'tkazildi.",
                'student_id': id,
            })

        messages.success(request, f"O'quvchi '{st_full_name}' arxivga muvaffaqiyatli o'tkazildi.")
        next_url = request.POST.get('next') or request.GET.get('next')
        if next_url:
            return redirect(next_url)
        if group_id:
            return redirect('admin_group_detail', id=group_id)
        return redirect('admin_dashboard')

    return redirect(request.GET.get('next') or 'admin_dashboard')


@admin_required
def admin_deleted_students(request):
    if not _admin_only(request):
        return redirect('home')
    deleted = DeletedStudent.objects.order_by('-deleted_at')
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
            student = Student.objects.filter(id=ds.student_id).first() if ds.student_id else None
            if student:
                student.is_active = True
                student.left_date = None
                student.time_slot = ts
                student.save()
            else:
                name_parts = (ds.student_name or '').split()
                phone_parts = [p.strip() for p in (ds.phones or '').split(',')]
                Student.objects.create(
                    first_name=ds.first_name or (name_parts[0] if name_parts else "Ism"),
                    last_name=ds.last_name or (name_parts[1] if len(name_parts) > 1 else ""),
                    age=ds.age or 0,
                    phone_1=ds.phone_1 or (phone_parts[0] if phone_parts else ""),
                    phone_2=ds.phone_2 or (phone_parts[1] if len(phone_parts) > 1 else ""),
                    time_slot=ts,
                    is_active=True,
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
    """Admin tomonidan o'quvchi ma'lumotlarini (jumladan guruh/dars vaqti va faolligini) tahrirlash."""
    if not _admin_only(request):
        return redirect('home')
    student = get_object_or_404(Student, id=id)
    next_url = request.GET.get('next') or request.POST.get('next', '')
    timeslots = TimeSlot.objects.select_related('group', 'group__teacher').order_by('group__name', 'start_time')

    if request.method == 'POST':
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        age_raw = request.POST.get('age', '').strip()
        phone_1 = request.POST.get('phone_1', '').strip()
        phone_2 = request.POST.get('phone_2', '').strip()
        payment_rate_raw = request.POST.get('payment_rate', '').strip()
        timeslot_id_raw = request.POST.get('time_slot_id', '').strip()

        if contains_cyrillic(first_name) or contains_cyrillic(last_name):
            messages.error(request, "Ism va familiyada faqat lotin alifbosidan foydalaning!")
        else:
            student.first_name = first_name or student.first_name
            student.last_name = last_name or student.last_name
            student.age = int(age_raw) if age_raw.isdigit() else student.age
            student.phone_1 = phone_1 or student.phone_1
            student.phone_2 = phone_2
            if payment_rate_raw.isdigit():
                student.payment_rate = int(payment_rate_raw)

            if timeslot_id_raw and timeslot_id_raw.isdigit():
                student.time_slot = get_object_or_404(TimeSlot, id=int(timeslot_id_raw))

            if 'is_active' in request.POST:
                student.is_active = request.POST.get('is_active') in ('1', 'on', 'true', True)
            elif student.time_slot_id:
                student.is_active = True

            # Agar o'quvchi faol va guruhga biriktirilgan bo'lsa, eski ketgan sana va arxiv qoldiqlarini tozalaymiz
            if student.is_active:
                student.left_date = None
                DeletedStudent.objects.filter(student_id=student.id).delete()

            joined_date_raw = request.POST.get('joined_date', '').strip()
            if joined_date_raw:
                try:
                    student.joined_date = _parse_past_or_today_date(joined_date_raw)
                except ValueError as err:
                    if str(err) == 'future':
                        messages.error(request, "Kelgan sana kelajakdagi sana bo'lishi mumkin emas! Bugungi yoki o'tgan sanani kiriting.")
                        return render(request, 'student_edit.html', {
                            'student': student, 'next': next_url, 'role': 'admin', 'timeslots': timeslots,
                        })

            if request.POST.get('form_type') == 'full_edit' or 'is_local' in request.POST:
                student.is_local = request.POST.get('is_local') in ('1', 'on', 'true', True)
            student.save()
            messages.success(request, "O'quvchi ma'lumotlari yangilandi.")
            if next_url:
                return redirect(next_url)
            if student.time_slot:
                return redirect('admin_group_detail', id=student.time_slot.group_id)
            return redirect('admin_dashboard')

    return render(request, 'student_edit.html', {
        'student': student,
        'next': next_url,
        'role': 'admin',
        'timeslots': timeslots,
    })


# ─────────────────────────────────────────────────────────────
# 6. VAQTINCHALIK O'QUVCHILAR (TEMP STUDENTS)
# ─────────────────────────────────────────────────────────────

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

    return render(request, 'admin_temp_students.html', {
        'form': form,
        'temp_students': TemporaryStudent.objects.all(),
        'timeslots': TimeSlot.objects.select_related('group', 'group__teacher').all(),
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
        get_object_or_404(TemporaryStudent, id=id).delete()
    return redirect('admin_temp_students')


# ─────────────────────────────────────────────────────────────
# 7. UMUMIY (SHARED): KELGAN SANANI YANGILASH VA RASM YUKLASH
# ─────────────────────────────────────────────────────────────

def _check_student_access(request, student: Student, allow_session_admin: bool = False):
    """Foydalanuvchi (Admin yoki shu guruhning O'qituvchisi) o'quvchini o'zgartira olishini tekshiradi."""
    is_admin = bool(request.session.get('admin_authenticated')) if allow_session_admin else bool(_admin_only(request))
    is_teacher = request.user.is_authenticated and getattr(request.user, 'role', '') == 'teacher'
    if not is_admin and not is_teacher:
        return False, "Ruxsat berilmagan"
    if is_teacher and not is_admin:
        owns_student = bool(
            student.time_slot
            and student.time_slot.group
            and student.time_slot.group.teacher
            and student.time_slot.group.teacher.user == request.user
        )
        if not owns_student:
            return False, "Bu o'quvchi sizning guruhingizga tegishli emas"
    return True, None


def update_student_joined_date(request, id):
    """Admin yoki o'qituvchi tomonidan kelgan sanani yangilash (AJAX)."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': "Noto'g'ri so'rov"}, status=400)

    student = get_object_or_404(Student, id=id)
    allowed, err_msg = _check_student_access(request, student)
    if not allowed:
        return JsonResponse({'status': 'error', 'msg': err_msg}, status=403)

    joined_date_raw = request.POST.get('joined_date', '').strip()
    if not joined_date_raw:
        return JsonResponse({'status': 'error', 'msg': "Kelgan sana kiritilmadi"}, status=400)

    try:
        new_date = _parse_past_or_today_date(joined_date_raw)
    except ValueError as err:
        if str(err) == 'future':
            return JsonResponse({
                'status': 'error',
                'msg': "Kelgan sana kelajakdagi sana bo'lishi mumkin emas! Bugungi yoki o'tgan sanani kiriting.",
            }, status=400)
        return JsonResponse({'status': 'error', 'msg': "Sana formati noto'g'ri (YYYY-MM-DD)"}, status=400)

    student.joined_date = new_date
    student.save(update_fields=['joined_date'])
    return JsonResponse({
        'status': 'success',
        'student_id': student.id,
        'joined_date': new_date.strftime('%Y-%m-%d'),
        'display_date': new_date.strftime('%d.%m.%Y'),
        'msg': "Kelgan sana muvaffaqiyatli saqlandi!",
    })


def student_photo_upload(request, id):
    """O'quvchiga rasm yuklash / yangilash."""
    student = get_object_or_404(Student, id=id)
    allowed, err_msg = _check_student_access(request, student, allow_session_admin=True)
    if not allowed:
        return JsonResponse({'status': 'error', 'msg': err_msg}, status=403)

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'msg': "Noto'g'ri so'rov usuli"}, status=405)

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
    face_linked = sync_staff_face_embedding(student)
    return JsonResponse({
        'status': 'success',
        'photo_url': student.photo.url,
        'face_detected': face_linked,
    })


# ─────────────────────────────────────────────────────────────
# 8. ADMIN CHAT
# ─────────────────────────────────────────────────────────────

def _get_admin_user(request):
    if request.user.is_authenticated and getattr(request.user, 'role', '') == 'admin':
        return request.user
    return CustomUser.objects.filter(role='admin').first()


@admin_required
def admin_chat_list(request):
    admin_user = _get_admin_user(request)
    teachers = Teacher.objects.select_related('user').all()
    unread_map = {}
    if admin_user:
        unread_qs = (
            Message.objects.filter(receiver=admin_user, is_read=False)
            .values('sender_id')
            .annotate(cnt=Count('id'))
        )
        unread_map = {item['sender_id']: item['cnt'] for item in unread_qs}

    teacher_data = [{'teacher': t, 'unread': unread_map.get(t.user_id, 0)} for t in teachers]
    return render(request, 'admin_chat_list.html', {'teacher_data': teacher_data})


@admin_required
def admin_chat_detail(request, teacher_id):
    admin_user = _get_admin_user(request)
    teacher = get_object_or_404(Teacher.objects.select_related('user'), id=teacher_id)
    t_user = teacher.user

    if admin_user:
        Message.objects.filter(sender=t_user, receiver=admin_user, is_read=False).update(is_read=True)

    messages_qs = (
        Message.objects.filter(
            (Q(sender=admin_user) & Q(receiver=t_user))
            | (Q(sender=t_user) & Q(receiver=admin_user))
        ).order_by('timestamp')
        if admin_user else Message.objects.none()
    )

    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        if content and admin_user:
            Message.objects.create(sender=admin_user, receiver=t_user, content=content)
            return redirect('admin_chat_detail', teacher_id=teacher_id)

    return render(request, 'admin_chat_detail.html', {
        'chat_user': t_user,
        'teacher': teacher,
        'chat_messages': messages_qs,
    })

