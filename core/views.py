"""
HUMO O'quv markazi — Core Views
Clean Architecture: modullashtirilgan view fayllar:
- auth_views.py: Kirish, chiqish, OTP, ota-onalar ko'rinishi
- admin_views.py: Admin paneli, guruhlar, to'lovlar, Excel eksport, chat
- teacher_views.py: O'qituvchi jurnali, davomat, o'quvchilar, chat
"""
from .auth_views import *
from .admin_views import *
from .teacher_views import *
