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

def admin_required(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.session.get('admin_authenticated'):
            return redirect('admin_login')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def _admin_only(request):
    return request.session.get('admin_authenticated')


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
# TEACHER AUTH
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


# ─────────────────────────────────────────────
# PARENT VIEW
# ─────────────────────────────────────────────

def parent_view(request):
    groups = Group.objects.prefetch_related(
        'time_slots__students__attendances'
    ).select_related('teacher')
    return render(request, 'parent_view.html', {'groups': groups})
