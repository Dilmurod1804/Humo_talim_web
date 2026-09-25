from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),

    # ── Admin Auth ──────────────────────────────────────────────────────────
    path('admin-login/', views.admin_login, name='admin_login'),
    path('admin-logout/', views.admin_logout, name='admin_logout'),

    # ── Admin Panel ──────────────────────────────────────────────────────────
    path('admin/dashboard/',         views.admin_dashboard,        name='admin_dashboard'),
    path('admin/group/create/',      views.admin_group_create,     name='admin_group_create'),
    path('admin/group/<int:id>/',    views.admin_group_detail,     name='admin_group_detail'),
    path('admin/group/<int:id>/delete/', views.admin_group_delete,  name='admin_group_delete'),
    path('admin/teacher/create/',    views.admin_teacher_create,   name='admin_teacher_create'),
    path('admin/teacher/<int:id>/delete/', views.admin_teacher_delete, name='admin_teacher_delete'),
    path('admin/teacher/<int:id>/toggle-edit/', views.admin_teacher_toggle_edit, name='admin_teacher_toggle_edit'),
    path('admin/deleted-students/',  views.admin_deleted_students, name='admin_deleted_students'),
    path('admin/deleted-students/<int:id>/restore/', views.admin_deleted_student_restore, name='admin_deleted_student_restore'),
    path('admin/deleted-students/<int:id>/delete/', views.admin_deleted_student_delete_permanent, name='admin_deleted_student_delete_permanent'),
    path('admin/export/',            views.admin_export_excel,       name='admin_export_excel'),
    path('admin/timeslot/<int:timeslot_id>/student/create/', views.admin_student_create, name='admin_student_create'),
    path('admin/timeslot/<int:id>/delete/', views.admin_timeslot_delete, name='admin_timeslot_delete'),
    path('admin/student/<int:id>/delete/', views.admin_student_delete, name='admin_student_delete'),
    path('admin/student/<int:id>/edit/', views.admin_student_edit, name='admin_student_edit'),
    path('admin/student/<int:id>/photo/', views.student_photo_upload, name='student_photo_upload'),
    path('admin/temp-students/',                     views.admin_temp_students,       name='admin_temp_students'),
    path('admin/temp-students/<int:id>/assign/',     views.admin_temp_student_assign, name='admin_temp_student_assign'),
    path('admin/temp-students/<int:id>/delete/',     views.admin_temp_student_delete, name='admin_temp_student_delete'),
    path('admin/attendance/',                        views.admin_attendance_mark,     name='admin_attendance_mark'),
    path('admin/payment/',                           views.payment_mark,              name='payment_mark'),

    # ── Teacher ──────────────────────────────────────────────────────────────
    path('teacher/login/',              views.teacher_login,           name='teacher_login'),
    path('teacher/dashboard/',          views.teacher_dashboard,       name='teacher_dashboard'),
    path('teacher/attendance/',         views.teacher_attendance_mark, name='teacher_attendance_mark'),
    path('teacher/timeslot/<int:timeslot_id>/student/create/', views.teacher_student_create, name='teacher_student_create'),
    path('teacher/student/<int:id>/delete/', views.teacher_student_delete, name='teacher_student_delete'),
    path('teacher/student/<int:id>/edit/', views.teacher_student_edit, name='teacher_student_edit'),
    path('teacher/student/<int:id>/photo/', views.student_photo_upload, name='teacher_student_photo_upload'),
    path('teacher/chat/',               views.teacher_chat,            name='teacher_chat'),

    # ── Admin Chat ───────────────────────────────────────────────────────────
    path('admin/chat/',                 views.admin_chat_list,         name='admin_chat_list'),
    path('admin/chat/<int:teacher_id>/', views.admin_chat_detail,      name='admin_chat_detail'),

    # ── Parent ───────────────────────────────────────────────────────────────
    path('parent/view/', views.parent_view, name='parent_view'),

    # ── Auth ──────────────────────────────────────────────────────────────────
    path('logout/', views.user_logout, name='logout'),
]
