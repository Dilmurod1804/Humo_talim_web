"""
HUMO O'quv markazi — Context Processors
Shablonlarda Admin va Menejer rollarini aniqlash uchun global o'zgaruvchilar.
"""

def auth_roles(request):
    """
    Global shablon o'zgaruvchilari:
    - is_admin: True faqat tizim administratori bo'lsa
    - is_manager: True faqat tizimga kirgan foydalanuvchi roli 'manager' bo'lsa
    - is_staff_manager_or_admin: True agar foydalanuvchi Admin yoki Menejer bo'lsa
    """
    if not request:
        return {
            'is_admin': False,
            'is_manager': False,
            'is_staff_manager_or_admin': False,
        }

    is_admin = bool(
        request.session.get('admin_authenticated') or
        (
            getattr(request, 'user', None)
            and request.user.is_authenticated
            and (getattr(request.user, 'role', '') == 'admin' or request.user.is_superuser)
        )
    )

    is_manager = bool(
        getattr(request, 'user', None)
        and request.user.is_authenticated
        and getattr(request.user, 'role', '') == 'manager'
    )

    is_assigned_teacher = False
    is_unassigned_staff = False
    assigned_group = None

    if getattr(request, 'user', None) and request.user.is_authenticated:
        if request.user.role == 'teacher':
            tp = getattr(request.user, 'teacher_profile', None)
            if tp and tp.groups.exists():
                is_assigned_teacher = True
                assigned_group = tp.groups.first()
            else:
                is_unassigned_staff = True
        elif request.user.role == 'manager':
            # Menejer operatsion boshqaruv huquqiga ega
            is_unassigned_staff = False

    return {
        'is_admin': is_admin,
        'is_manager': is_manager,
        'is_staff_manager_or_admin': is_admin or is_manager,
        'is_assigned_teacher': is_assigned_teacher,
        'is_unassigned_staff': is_unassigned_staff,
        'assigned_group': assigned_group,
    }

