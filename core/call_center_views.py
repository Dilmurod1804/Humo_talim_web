"""
HUMO O'quv markazi — Call Center / Qarzdorlar Bilan Ishlash Moduli (call_center_views.py)

Ushbu modul moliya va to'lovlar bo'limidan butunlay alohida (izolyatsiyalangan) bo'lib,
menejerlarga oylik qarzdor o'quvchilar ro'yxati bo'yicha tezkor qo'ng'iroq qilish,
click-to-call (HTML tel: protokoli) orqali bog'lanish va qo'ng'iroq holatlarini
("Kutilmoqda", "Xabar qilindi", "Qayta chaqiruv") qayd etish imkonini beradi.
"""
from datetime import date
from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .auth_views import manager_or_admin_required
from .models import DebtorCallLog, Group, Student
from .payment_views import (
    _build_payments_data,
    _fmt_money,
    get_active_students_qs,
    normalize_month_str,
)


@manager_or_admin_required
def debtors_call_center(request):
    """
    Call Center asosiy sahifasi:
    Joriy yoki tanlangan oy bo'yicha to'lov qilmagan qarzdor o'quvchilar ro'yxati.
    Asosiy to'lovlar paneli (admin_payments_all) bilan AYNAN BIR XIL hisob-kitob
    va filtrlash logikasidan (_build_payments_data) foydalanadi.
    """
    req_month, year, mon = normalize_month_str(request.GET.get('month'))
    search_q = request.GET.get('q', '').strip()
    group_id = request.GET.get('group_id', '').strip()
    status_filter = (request.GET.get('status') or 'all').strip().lower()

    # Guruhlar ro'yxati filtrlash uchun
    groups = Group.objects.select_related('teacher').order_by('name')

    # Faol o'quvchilarni olish
    students_qs = get_active_students_qs(
        group_id=int(group_id) if group_id.isdigit() else None,
        search_q=search_q,
    )

    # Asosiy to'lovlar paneli bilan 100% bir xil logikada qarzdorlarni olish
    rows, stats = _build_payments_data(
        students_qs,
        req_month,
        status_filter='unpaid',
        is_group_specific=group_id.isdigit(),
    )

    student_ids = [r['student_id'] for r in rows]

    # Ushbu oy uchun qo'ng'iroq holatlari
    call_logs = DebtorCallLog.objects.filter(
        student_id__in=student_ids,
        month=req_month,
    ).select_related('called_by')
    call_logs_map = {c.student_id: c for c in call_logs}

    debtors = []
    total_debt_sum = 0
    counts = {
        'all': len(rows),
        'pending': 0,
        'notified': 0,
        'no_answer': 0,
    }

    for r in rows:
        debt_amount = r.get('debt', 0)
        total_debt_sum += debt_amount

        sid = r['student_id']
        call_log = call_logs_map.get(sid)
        current_status = call_log.status if call_log else 'pending'
        counts[current_status] = counts.get(current_status, 0) + 1

        # Qo'ng'iroq holati bo'yicha filter (agar tanlangan bo'lsa)
        if status_filter in ('pending', 'notified', 'no_answer') and current_status != status_filter:
            continue

        s = r['student']
        debtors.append({
            'student': s,
            'student_id': sid,
            'student_name': r['student_name'],
            'group_name': r['group_name'],
            'teacher_name': r['teacher_name'],
            'debt_amount': debt_amount,
            'debt_amount_formatted': r.get('debt_formatted', _fmt_money(debt_amount)),
            'payment_rate_formatted': r.get('payment_rate_formatted', '0'),
            'call_status': current_status,
            'call_status_display': dict(DebtorCallLog.STATUS_CHOICES).get(current_status, "Kutilmoqda"),
            'called_at': call_log.called_at if call_log else None,
            'called_by_name': (
                f"{call_log.called_by.first_name} {call_log.called_by.last_name}".strip()
                or call_log.called_by.username
            ) if (call_log and call_log.called_by) else "",
            'note': call_log.note if call_log else "",
            'is_overdue': r.get('is_overdue', False),
            'delay_text': r.get('delay_text', ''),
        })

    # Saralash: Avval javob bermagan va kutilayotganlar, keyin xabar qilinganlar
    status_order = {'no_answer': 0, 'pending': 1, 'notified': 2}
    debtors.sort(key=lambda item: (status_order.get(item['call_status'], 9), -item['debt_amount']))

    context = {
        'debtors': debtors,
        'counts': counts,
        'total_debt_sum': total_debt_sum,
        'total_debt_sum_formatted': _fmt_money(total_debt_sum),
        'current_month': req_month,
        'selected_status': status_filter,
        'selected_group_id': int(group_id) if group_id.isdigit() else '',
        'search_query': search_q,
        'groups': groups,
    }
    return render(request, 'call_center_debtors.html', context)


@manager_or_admin_required
def update_debtor_call_status(request):
    """
    Qarzdor o'quvchining qo'ng'iroq holatini yangilash ("notified", "no_answer", "pending").
    AJAX yoki standart form orqali chaqiriladi.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': "Faqat POST so'rovi qabul qilinadi."}, status=405)

    student_id = request.POST.get('student_id', '').strip()
    month_str = request.POST.get('month', '').strip()
    new_status = request.POST.get('status', '').strip().lower()
    note = request.POST.get('note')

    if not student_id or not str(student_id).isdigit():
        return JsonResponse({'success': False, 'message': "O'quvchi ID raqami noto'g'ri."}, status=400)

    if not month_str:
        month_str = date.today().strftime('%Y-%m')

    valid_statuses = dict(DebtorCallLog.STATUS_CHOICES)
    if new_status not in valid_statuses:
        return JsonResponse({'success': False, 'message': "Noto'g'ri qo'ng'iroq holati."}, status=400)

    student = get_object_or_404(Student, id=int(student_id))

    call_log, created = DebtorCallLog.objects.get_or_create(
        student=student,
        month=month_str,
        defaults={
            'status': new_status,
            'called_by': request.user if request.user.is_authenticated else None,
            'note': note.strip() if note else '',
        }
    )

    if not created:
        call_log.status = new_status
        if note is not None:
            call_log.note = note.strip()
        call_log.called_by = request.user if request.user.is_authenticated else None
        call_log.save()

    status_display = valid_statuses.get(new_status, new_status)
    caller_name = (
        f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
    ) if request.user.is_authenticated else "Tizim"

    msg = f"{student.first_name} {student.last_name} qo'ng'iroq holati: \"{status_display}\" deb belgilandi."

    # Yangilangan hisoblagichlarni qaytarish
    all_debtors, _ = _build_payments_data(get_active_students_qs(), month_str, status_filter='unpaid')
    debtor_ids = [d['student_id'] for d in all_debtors]
    logs = DebtorCallLog.objects.filter(student_id__in=debtor_ids, month=month_str)
    status_map = {l.student_id: l.status for l in logs}
    counts = {'all': len(debtor_ids), 'pending': 0, 'notified': 0, 'no_answer': 0}
    for sid in debtor_ids:
        st = status_map.get(sid, 'pending')
        counts[st] = counts.get(st, 0) + 1

    # AJAX so'rov uchun JSON
    if (
        request.headers.get('x-requested-with') == 'XMLHttpRequest'
        or request.POST.get('ajax') == '1'
        or 'application/json' in request.headers.get('Accept', '')
    ):
        return JsonResponse({
            'success': True,
            'message': msg,
            'student_id': student.id,
            'month': month_str,
            'status': new_status,
            'status_display': status_display,
            'called_by_name': caller_name,
            'called_at': call_log.called_at.strftime('%H:%M, %d.%m.%Y'),
            'note': call_log.note,
            'counts': counts,
        })

    messages.success(request, msg)
    next_url = request.POST.get('next') or request.META.get('HTTP_REFERER') or '/call-center/debtors/'
    return redirect(next_url)
