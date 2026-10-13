from django.urls import path
from . import auth_views, admin_views, teacher_views

urlpatterns = [
    # ── Home & Shared Auth ───────────────────────────────────────────────────
    path('', auth_views.home, name='home'),
    path('logout/', auth_views.user_logout, name='logout'),

    # ── Admin Auth ──────────────────────────────────────────────────────────
    path('admin-login/', auth_views.admin_login, name='admin_login'),
    path('admin-logout/', auth_views.admin_logout, name='admin_logout'),

    # ── Admin Panel ──────────────────────────────────────────────────────────
    path('admin/dashboard/',         admin_views.admin_dashboard,        name='admin_dashboard'),
    path('admin/group/create/',      admin_views.admin_group_create,     name='admin_group_create'),
    path('admin/group/<int:id>/',    admin_views.admin_group_detail,     name='admin_group_detail'),
    path('admin/group/<int:group_id>/export/', admin_views.admin_group_export_excel, name='admin_group_export_excel'),
    path('admin/group/<int:id>/delete/', admin_views.admin_group_delete,  name='admin_group_delete'),
    path('admin/teacher/create/',    admin_views.admin_teacher_create,   name='admin_teacher_create'),
    path('admin/teacher/<int:id>/delete/', admin_views.admin_teacher_delete, name='admin_teacher_delete'),
    path('admin/teacher/<int:id>/toggle-edit/', admin_views.admin_teacher_toggle_edit, name='admin_teacher_toggle_edit'),
    path('admin/teachers/toggle-all-edit/', admin_views.admin_teacher_toggle_all_edit, name='admin_teacher_toggle_all_edit'),
    path('admin/deleted-students/',  admin_views.admin_deleted_students, name='admin_deleted_students'),
    path('admin/deleted-students/<int:id>/restore/', admin_views.admin_deleted_student_restore, name='admin_deleted_student_restore'),
    path('admin/deleted-students/<int:id>/delete/', admin_views.admin_deleted_student_delete_permanent, name='admin_deleted_student_delete_permanent'),
    path('admin/export/',            admin_views.admin_export_excel,       name='admin_export_excel'),
    path('admin/timeslot/<int:timeslot_id>/student/create/', admin_views.admin_student_create, name='admin_student_create'),
    path('admin/timeslot/<int:id>/delete/', admin_views.admin_timeslot_delete, name='admin_timeslot_delete'),
    path('admin/student/<int:id>/delete/', admin_views.admin_student_delete, name='admin_student_delete'),
    path('admin/student/<int:id>/edit/', admin_views.admin_student_edit, name='admin_student_edit'),
    path('admin/student/<int:id>/photo/', admin_views.student_photo_upload, name='student_photo_upload'),
    path('student/<int:id>/joined-date/', admin_views.update_student_joined_date, name='update_student_joined_date'),
    path('admin/temp-students/',                     admin_views.admin_temp_students,       name='admin_temp_students'),
    path('admin/temp-students/<int:id>/assign/',     admin_views.admin_temp_student_assign, name='admin_temp_student_assign'),
    path('admin/temp-students/<int:id>/delete/',     admin_views.admin_temp_student_delete, name='admin_temp_student_delete'),
    path('admin/attendance/',                        admin_views.admin_attendance_mark,     name='admin_attendance_mark'),
    path('admin/payment/',                           admin_views.payment_mark,              name='payment_mark'),

    # ── Admin Payments ───────────────────────────────────────────────────────
    path('admin/payments/',                          admin_views.admin_payments_all,        name='admin_payments_all'),
    path('admin/group/<int:group_id>/payments/',     admin_views.admin_group_payments,      name='admin_group_payments'),
    path('admin/payment/save/',                      admin_views.admin_payment_save,        name='admin_payment_save'),
    path('admin/payment/<int:id>/delete/',           admin_views.admin_payment_delete,      name='admin_payment_delete'),
    path('admin/payments/export/',                   admin_views.export_payments_excel,     name='export_payments_excel'),

    # ── Teacher ──────────────────────────────────────────────────────────────
    path('teacher/login/',              auth_views.teacher_login,           name='teacher_login'),
    path('teacher/dashboard/',          teacher_views.teacher_dashboard,       name='teacher_dashboard'),
    path('teacher/export/',             teacher_views.teacher_export_excel,    name='teacher_export_excel'),
    path('teacher/attendance/',         teacher_views.teacher_attendance_mark, name='teacher_attendance_mark'),
    path('teacher/timeslot/<int:timeslot_id>/student/create/', teacher_views.teacher_student_create, name='teacher_student_create'),
    path('teacher/student/<int:id>/delete/', teacher_views.teacher_student_delete, name='teacher_student_delete'),
    path('teacher/student/<int:id>/edit/', teacher_views.teacher_student_edit, name='teacher_student_edit'),
    path('teacher/student/<int:id>/photo/', admin_views.student_photo_upload, name='teacher_student_photo_upload'),
    path('teacher/chat/',               teacher_views.teacher_chat,            name='teacher_chat'),

    # ── Admin Chat ───────────────────────────────────────────────────────────
    path('admin/chat/',                 admin_views.admin_chat_list,         name='admin_chat_list'),
    path('admin/chat/<int:teacher_id>/', admin_views.admin_chat_detail,      name='admin_chat_detail'),

    # ── Parent ───────────────────────────────────────────────────────────────
    path('parent/view/', auth_views.parent_view, name='parent_view'),
]
