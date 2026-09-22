import re

with open('e:/HUNO_TA\'LIM/core/views.py', 'r', encoding='utf-8') as f:
    content = f.read()

new_admin_auth = '''# ─────────────────────────────────────────────
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

'''

content = re.sub(
    r'# ─────────────────────────────────────────────\n# ADMIN AUTH.*?(?=# ─────────────────────────────────────────────\n# ADMIN PANEL VIEWS)',
    new_admin_auth,
    content,
    flags=re.DOTALL
)

content = content.replace("@login_required(login_url='/admin-login/')", "@admin_required")

content = re.sub(
    r'def _admin_only\(request\):\n\s+return request\.user\.is_authenticated and request\.user\.role == \'admin\'',
    'def _admin_only(request):\n    return request.session.get(\'admin_authenticated\')',
    content
)

with open('e:/HUNO_TA\'LIM/core/views.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Done')
