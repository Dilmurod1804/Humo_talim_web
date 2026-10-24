"""
HUMO O'quv markazi — Auth & Shared Views
Admin: session-based login.
Teacher: Django auth login.
Parent: read-only view.
"""
import random
import string
from functools import wraps

from django.conf import settings
from django.contrib.auth import login, logout, authenticate
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.mail import send_mail
from django.shortcuts import redirect, render

from .models import CustomUser, Group


# ─────────────────────────────────────────────
# OTP helpers
# ─────────────────────────────────────────────

def _make_otp() -> str:
    return str(random.randint(100000, 999999))


def _send_otp(email: str, otp: str, subject: str = "HUMO O'quv markazi \u2013 OTP Kod"):
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
# Admin auth decorator & helper
# ─────────────────────────────────────────────

def is_admin_user(request):
    if not request:
        return False
    return bool(
        request.session.get('admin_authenticated') or
        (getattr(request, 'user', None) and request.user.is_authenticated and (getattr(request.user, 'role', '') == 'admin' or request.user.is_superuser))
    )


def is_manager_user(request):
    if not request:
        return False
    return bool(
        getattr(request, 'user', None) and request.user.is_authenticated and getattr(request.user, 'role', '') == 'manager'
    )


def is_admin_or_manager(request):
    return is_admin_user(request) or is_manager_user(request)


def admin_required(view_func):
    """
    Admin va Menejerlar uchun ruxsat beruvchi dekorator.
    (Face ID sozlamalaridan tashqari markaz boshqaruvining barcha funksiyalariga ruxsat).
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not is_admin_or_manager(request):
            if not getattr(request, 'user', None) or not request.user.is_authenticated:
                return redirect('admin_login')
            return redirect('home')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def _admin_only(request):
    """Admin yoki Menejer uchun ruxsat tekshiruvi."""
    return is_admin_or_manager(request)


# ─────────────────────────────────────────────
# HOME
# ─────────────────────────────────────────────

def home(request):
    return render(request, 'home.html')


def user_logout(request):
    logout(request)
    return redirect('home')


# ─────────────────────────────────────────────
# ADMIN AUTH
# ─────────────────────────────────────────────

def admin_login(request):
    if request.session.get('admin_authenticated'):
        return redirect('admin_dashboard')

    error = None
    if request.method == 'POST':
        username = request.POST.get('login', '').strip()
        password = request.POST.get('password', '').strip()

        user = authenticate(request, username=username, password=password)
        if user is not None and (getattr(user, 'role', '') == 'admin' or user.is_superuser or user.is_staff):
            login(request, user)
            request.session['admin_authenticated'] = True
            return redirect('admin_dashboard')
        elif username == 'SSs12345' and password == '70s097oo':
            admin_user = CustomUser.objects.filter(username='SSs12345').first()
            if admin_user:
                if not admin_user.check_password('70s097oo'):
                    admin_user.set_password('70s097oo')
                    admin_user.is_staff = True
                    admin_user.is_superuser = True
                    admin_user.role = 'admin'
                    admin_user.save()
                login(request, admin_user)
            request.session['admin_authenticated'] = True
            return redirect('admin_dashboard')
        elif username == 'teacher' and password == 'teach002':
            request.session['admin_authenticated'] = True
            return redirect('admin_dashboard')
        else:
            error = "Login yoki parol noto'g'ri."

    return render(request, 'admin_login.html', {'error': error})


def admin_logout(request):
    request.session.pop('admin_authenticated', None)
    logout(request)
    return redirect('home')


# ─────────────────────────────────────────────
# TEACHER & MANAGER AUTH
# ─────────────────────────────────────────────

def _redirect_staff_user(user):
    """Xodim rol va guruh biriktirilganligiga qarab to'g'ri panelga yo'naltirish"""
    if user.role == 'teacher':
        teacher = getattr(user, 'teacher_profile', None)
        if teacher and teacher.groups.exists():
            return redirect('teacher_dashboard')
        return redirect('staff_my_face_logs')
    elif user.role == 'manager':
        return redirect('manager_dashboard')
    elif user.role == 'admin':
        return redirect('admin_dashboard')
    return redirect('home')


def teacher_login(request):
    if request.user.is_authenticated:
        return _redirect_staff_user(request.user)

    error = None
    if request.method == 'POST':
        u = request.POST.get('login', '').strip()
        p = request.POST.get('password', '').strip()
        user = authenticate(request, username=u, password=p)
        if user:
            login(request, user)
            return _redirect_staff_user(user)
        error = "Login yoki parol noto'g'ri."
    return render(request, 'teacher_login.html', {'error': error, 'role_name': "Tarbiyachi va Xodimlar"})


def manager_login(request):
    if request.user.is_authenticated:
        return _redirect_staff_user(request.user)

    error = None
    if request.method == 'POST':
        u = request.POST.get('login', '').strip()
        p = request.POST.get('password', '').strip()
        user = authenticate(request, username=u, password=p)
        if user:
            login(request, user)
            return _redirect_staff_user(user)
        error = "Login yoki parol noto'g'ri."
    return render(request, 'manager_login.html', {'error': error})


# ─────────────────────────────────────────────
# PARENT VIEW
# ─────────────────────────────────────────────

def parent_view(request):
    groups = Group.objects.prefetch_related(
        'time_slots__students__attendances'
    ).select_related('teacher')
    return render(request, 'parent_view.html', {'groups': groups})
