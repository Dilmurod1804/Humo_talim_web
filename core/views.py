"""
HUMO O'quv markazi — Core Views
Clean Architecture — modullashtirilgan view fayllar:
  - auth_views.py    : Kirish, chiqish, OTP, rol dekoratorlari va ota-onalar ko'rinishi
  - student_views.py : Admin dashboard, guruhlar, davomat, o'quvchilar, arxiv va chat
  - payment_views.py : To'lovlar, qarzdorlar ORM filtrlari, statistika va Excel eksport
  - face_id_views.py : Xodimlar, Face ID terminal/API, oylik maosh, bonus, avans va audit
  - teacher_views.py : O'qituvchi jurnali, davomat, o'quvchilar va chat
"""
from .auth_views import *     # noqa: F401,F403
from .student_views import *  # noqa: F401,F403
from .payment_views import *  # noqa: F401,F403
from .face_id_views import *  # noqa: F401,F403
from .teacher_views import *  # noqa: F401,F403
