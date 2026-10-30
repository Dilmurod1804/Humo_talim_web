from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.utils.html import format_html
from django.contrib import messages
import os

from .models import (
    CustomUser, Teacher, Manager, Group, TimeSlot,
    Student, Attendance, DeletedStudent, MonthlyPayment,
    FaceIDSettings, FaceIDLog
)
try:
    import core.face_service as fs
    HAS_OPENCV = getattr(fs, 'HAS_OPENCV', False)
except Exception:
    fs = None
    HAS_OPENCV = False


# ─────────────────────────────────────────────────────────────
# 1. CUSTOM USER ADMIN
# ─────────────────────────────────────────────────────────────

@admin.action(description="Tanlangan foydalanuvchilarni Face ID tizimiga sinxronlash")
def sync_users_to_face_id(modeladmin, request, queryset):
    settings_obj = FaceIDSettings.get_settings()
    created_profiles = 0
    synced_faces = 0
    for user in queryset:
        if user.role == 'teacher' and not hasattr(user, 'teacher_profile'):
            Teacher.objects.create(
                user=user,
                first_name=user.first_name or user.username,
                last_name=user.last_name or '',
                generated_login=user.username,
                generated_password='***',
                work_start_time=settings_obj.default_work_start_time,
                work_end_time=settings_obj.default_work_end_time,
                late_fine_rate=settings_obj.default_late_fine_rate
            )
            created_profiles += 1
        elif user.role == 'manager' and not hasattr(user, 'manager_profile'):
            Manager.objects.create(
                user=user,
                first_name=user.first_name or user.username,
                last_name=user.last_name or '',
                generated_login=user.username,
                generated_password='***',
                work_start_time=settings_obj.default_work_start_time,
                work_end_time=settings_obj.default_work_end_time,
                late_fine_rate=settings_obj.default_late_fine_rate
            )
            created_profiles += 1

        if fs and getattr(user, 'role', '') in ('teacher', 'manager'):
            if fs.sync_staff_face_embedding(user):
                synced_faces += 1

    messages.success(
        request,
        f"{queryset.count()} ta foydalanuvchi tekshirildi, {created_profiles} ta yangi profil yaratildi, "
        f"{synced_faces} ta yuz biometrik matritsasi yangilandi."
    )


class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'staff_display_name', 'role', 'face_id_status', 'is_active', 'is_staff')
    list_filter = ('role', 'is_active', 'is_staff')
    actions = [sync_users_to_face_id]

    fieldsets = UserAdmin.fieldsets + (
        ('HUMO Sozlamalari', {'fields': ('role', 'has_custom_credentials')}),
    )

    def staff_display_name(self, obj):
        return obj.staff_full_name
    staff_display_name.short_description = "F.I.SH"

    def face_id_status(self, obj):
        prof = getattr(obj, 'teacher_profile', None) if obj.role == 'teacher' else (
            getattr(obj, 'manager_profile', None) if obj.role == 'manager' else obj.staff_profile
        )
        if not prof:
            return format_html('<span style="color:#94a3b8;">Profil yo\'q</span>')
        if getattr(prof, 'has_face_embedding', False):
            return format_html('<span style="color:#10b981; font-weight:bold;">✔ Face ID Faol</span>')
        if prof.photo:
            try:
                if os.path.exists(prof.photo.path):
                    if fs and fs.get_or_create_face_embedding(prof) is not None:
                        return format_html('<span style="color:#10b981; font-weight:bold;">✔ Face ID Faol</span>')
                    return format_html('<span style="color:#10b981; font-weight:bold;">✔ Yuz yuklangan</span>')
            except Exception:
                pass
            return format_html('<span style="color:#f59e0b; font-weight:bold;">⚠ Fayl topilmadi</span>')
        return format_html('<span style="color:#ef4444; font-weight:bold;">✖ Rasm yo\'q</span>')
    face_id_status.short_description = "Face ID Holati"


# ─────────────────────────────────────────────────────────────
# 2. TEACHER ADMIN
# ─────────────────────────────────────────────────────────────

@admin.action(description="Tanlangan o'qituvchilarni Face ID tizimiga sinxronlash va sozlash")
def sync_teachers_face_id(modeladmin, request, queryset):
    settings_obj = FaceIDSettings.get_settings()
    updated = 0
    for teacher in queryset:
        if not teacher.work_start_time:
            teacher.work_start_time = settings_obj.default_work_start_time
        if not teacher.work_end_time:
            teacher.work_end_time = settings_obj.default_work_end_time
        if not teacher.late_fine_rate:
            teacher.late_fine_rate = settings_obj.default_late_fine_rate
        teacher.save()
        if fs and teacher.photo:
            fs.sync_staff_face_embedding(teacher)
        updated += 1
    messages.success(request, f"{updated} ta o'qituvchi Face ID tizimiga muvaffaqiyatli sinxronlandi.")


@admin.register(Teacher)
class TeacherAdmin(admin.ModelAdmin):
    list_display = ('photo_preview', 'first_name', 'last_name', 'phone', 'work_start_time', 'monthly_salary', 'face_id_biometric_badge', 'is_active')
    list_filter = ('is_active', 'can_edit_students')
    search_fields = ('first_name', 'last_name', 'phone', 'user__username')
    actions = [sync_teachers_face_id]

    def photo_preview(self, obj):
        if obj.photo:
            try:
                return format_html('<img src="{}" style="width:38px; height:38px; border-radius:8px; object-fit:cover;" />', obj.photo.url)
            except Exception:
                pass
        return format_html('<div style="width:38px; height:38px; border-radius:8px; background:#e2e8f0; display:flex; align-items:center; justify-content:center; color:#94a3b8; font-size:11px;">Rasm yo\'q</div>')
    photo_preview.short_description = "Yuz Rasmi"

    def face_id_biometric_badge(self, obj):
        if getattr(obj, 'has_face_embedding', False):
            return format_html('<span style="background:#d1fae5; color:#047857; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">✔ Face ID Faol</span>')
        if not obj.photo:
            return format_html('<span style="background:#fee2e2; color:#b91c1c; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">Rasm yo\'q</span>')
        try:
            if not os.path.exists(obj.photo.path):
                return format_html('<span style="background:#fef3c7; color:#b45309; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">Fayl topilmadi</span>')
            if HAS_OPENCV and fs:
                if fs.get_or_create_face_embedding(obj) is not None:
                    return format_html('<span style="background:#d1fae5; color:#047857; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">✔ Face ID Faol</span>')
            return format_html('<span style="background:#fef3c7; color:#b45309; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">⚠ Yuz aniq emas</span>')
        except Exception:
            return format_html('<span style="background:#fee2e2; color:#b91c1c; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">Xatolik</span>')
    face_id_biometric_badge.short_description = "Face ID Holati"


# ─────────────────────────────────────────────────────────────
# 3. MANAGER ADMIN
# ─────────────────────────────────────────────────────────────

@admin.action(description="Tanlangan menejerlarni Face ID tizimiga sinxronlash")
def sync_managers_face_id(modeladmin, request, queryset):
    settings_obj = FaceIDSettings.get_settings()
    for m in queryset:
        if not m.work_start_time:
            m.work_start_time = settings_obj.default_work_start_time
        if not m.work_end_time:
            m.work_end_time = settings_obj.default_work_end_time
        m.save()
        if fs and m.photo:
            fs.sync_staff_face_embedding(m)
    messages.success(request, f"{queryset.count()} ta menejer Face ID tizimiga sinxronlandi.")


@admin.register(Manager)
class ManagerAdmin(admin.ModelAdmin):
    list_display = ('photo_preview', 'first_name', 'last_name', 'phone', 'work_start_time', 'monthly_salary', 'face_id_biometric_badge', 'is_active')
    list_filter = ('is_active',)
    search_fields = ('first_name', 'last_name', 'phone', 'user__username')
    actions = [sync_managers_face_id]

    def photo_preview(self, obj):
        if obj.photo:
            try:
                return format_html('<img src="{}" style="width:38px; height:38px; border-radius:8px; object-fit:cover;" />', obj.photo.url)
            except Exception:
                pass
        return format_html('<div style="width:38px; height:38px; border-radius:8px; background:#e2e8f0; display:flex; align-items:center; justify-content:center; color:#94a3b8; font-size:11px;">Rasm yo\'q</div>')
    photo_preview.short_description = "Yuz Rasmi"

    def face_id_biometric_badge(self, obj):
        if getattr(obj, 'has_face_embedding', False):
            return format_html('<span style="background:#d1fae5; color:#047857; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">✔ Face ID Faol</span>')
        if not obj.photo:
            return format_html('<span style="background:#fee2e2; color:#b91c1c; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">Rasm yo\'q</span>')
        try:
            if not os.path.exists(obj.photo.path):
                return format_html('<span style="background:#fef3c7; color:#b45309; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">Fayl topilmadi</span>')
            if HAS_OPENCV and fs:
                if fs.get_or_create_face_embedding(obj) is not None:
                    return format_html('<span style="background:#d1fae5; color:#047857; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">✔ Face ID Faol</span>')
            return format_html('<span style="background:#fef3c7; color:#b45309; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">⚠ Yuz aniq emas</span>')
        except Exception:
            return format_html('<span style="background:#fee2e2; color:#b91c1c; padding:3px 8px; border-radius:12px; font-weight:600; font-size:11px;">Xatolik</span>')
    face_id_biometric_badge.short_description = "Face ID Holati"


# ─────────────────────────────────────────────────────────────
# 4. FACE ID LOG ADMIN (Davomat Jurnali)
# ─────────────────────────────────────────────────────────────

@admin.register(FaceIDLog)
class FaceIDLogAdmin(admin.ModelAdmin):
    list_display = ('staff_photo', 'staff_name', 'staff_role', 'date', 'check_in_time', 'check_out_time', 'status_badge', 'late_minutes', 'fine_amount_deducted')
    list_filter = ('date', 'status', 'is_late', 'user__role')
    search_fields = ('user__username', 'user__first_name', 'user__last_name', 'notes')
    ordering = ('-date', '-check_in_time')

    def staff_photo(self, obj):
        prof = getattr(obj.user, 'staff_profile', None)
        if prof and getattr(prof, 'photo', None):
            try:
                return format_html('<img src="{}" style="width:36px; height:36px; border-radius:8px; object-fit:cover;" />', prof.photo.url)
            except Exception:
                pass
        return format_html('<div style="width:36px; height:36px; border-radius:8px; background:#6366f1; color:#fff; display:flex; align-items:center; justify-content:center; font-weight:bold; font-size:12px;">{}</div>', obj.user.username[:2].upper())
    staff_photo.short_description = "Avatar"

    def staff_name(self, obj):
        return obj.user.staff_full_name
    staff_name.short_description = "Xodim F.I.SH"

    def staff_role(self, obj):
        role = obj.user.role
        if role == 'manager':
            return format_html('<span style="background:#fef3c7; color:#b45309; padding:3px 8px; border-radius:10px; font-weight:bold; font-size:11px;">Menejer</span>')
        elif role == 'teacher':
            return format_html('<span style="background:#d1fae5; color:#047857; padding:3px 8px; border-radius:10px; font-weight:bold; font-size:11px;">O\'qituvchi</span>')
        return format_html('<span style="background:#f1f5f9; color:#475569; padding:3px 8px; border-radius:10px; font-size:11px;">{}</span>', obj.user.get_role_display())
    staff_role.short_description = "Lavozimi"

    def status_badge(self, obj):
        if obj.is_late:
            return format_html('<span style="background:#fee2e2; color:#b91c1c; padding:3px 8px; border-radius:10px; font-weight:bold; font-size:11px;">Kechikdi ({} daq)</span>', obj.late_minutes)
        return format_html('<span style="background:#d1fae5; color:#047857; padding:3px 8px; border-radius:10px; font-weight:bold; font-size:11px;">O\'z vaqtida</span>')
    status_badge.short_description = "Holati"


# ─────────────────────────────────────────────────────────────
# 5. FACE ID SETTINGS ADMIN
# ─────────────────────────────────────────────────────────────

@admin.register(FaceIDSettings)
class FaceIDSettingsAdmin(admin.ModelAdmin):
    list_display = ('center_name', 'latitude', 'longitude', 'allowed_radius_meters', 'default_work_start_time', 'default_work_end_time', 'is_geofencing_enabled', 'updated_at')


# ─────────────────────────────────────────────────────────────
# 6. BOSHQA ASOSIY MODELLAR
# ─────────────────────────────────────────────────────────────

admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(Group)
admin.site.register(TimeSlot)
admin.site.register(Student)
admin.site.register(Attendance)
admin.site.register(DeletedStudent)

@admin.register(MonthlyPayment)
class MonthlyPaymentAdmin(admin.ModelAdmin):
    list_display = ('student', 'month', 'amount_paid', 'payment_type', 'status', 'payment_date')
    list_filter = ('month', 'payment_type', 'status')
    search_fields = ('student__first_name', 'student__last_name', 'notes')
