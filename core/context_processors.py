"""
HUMO O'quv markazi — Context Processors
Shablonlarda Admin va Menejer rollarini aniqlash uchun global o'zgaruvchilar.
"""
from .auth_views import is_admin_user, is_manager_user, is_teacher_user


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
            'is_assigned_teacher': False,
            'is_unassigned_staff': False,
            'assigned_group': None,
        }

    is_admin = is_admin_user(request)
    is_manager = is_manager_user(request)

    is_assigned_teacher = False
    is_unassigned_staff = False
    assigned_group = None

    if getattr(request, 'user', None) and request.user.is_authenticated:
        if is_teacher_user(request):
            tp = getattr(request.user, 'teacher_profile', None)
            if tp and tp.groups.exists():
                is_assigned_teacher = True
                assigned_group = tp.groups.first()
            else:
                is_unassigned_staff = True
        elif is_manager:
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

