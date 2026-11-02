"""
Django Management Command: sync_face_id_staff
HUMO Ta'lim — Face ID Davomat Tizimiga Xodimlarni Sinxronlash va Bog'lash

Vazifasi:
1. Bazadagi barcha o'qituvchilar (Teacher), menejerlar (Manager) va foydalanuvchilarni (CustomUser) tekshirish.
2. Profil bog'lanishlarini to'g'irlash (Teacher <-> CustomUser, Manager <-> CustomUser).
3. Har bir xodimning Face ID yuz fotosuratini (diskdagi mavjudligi va OpenCV biometrik tahlili) tekshirish.
4. Standart ish vaqtlari va jarima stavkalarini sinxronlash.
5. Ixtiyoriy: --generate-sample-logs orqali davomat jurnalini sinov ma'lumotlari bilan to'ldirish.
"""

import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import CustomUser, Teacher, Manager, FaceIDSettings, FaceIDLog
try:
    import core.face_service as fs
    HAS_OPENCV = getattr(fs, 'HAS_OPENCV', False)
except Exception:
    fs = None
    HAS_OPENCV = False


class Command(BaseCommand):
    help = "Barcha o'qituvchi va xodimlarni Face ID davomat tizimiga sinxronlash va tekshirish"

    def add_arguments(self, parser):
        parser.add_argument(
            '--generate-sample-logs',
            action='store_true',
            help="Davomat jurnalida ko'rinishi uchun bugungi kunga sinov (namuna) Face ID yozuvlarini yaratish",
        )
        parser.add_argument(
            '--check-only',
            action='store_true',
            help="Faqat tekshirish rejimida ishlatish (o'zgartirishlar saqlanmaydi)",
        )

    def handle(self, *args, **options):
        generate_sample_logs = options.get('generate_sample_logs', False)
        check_only = options.get('check_only', False)

        self.stdout.write(self.style.MIGRATE_HEADING("==============================================================="))
        self.stdout.write(self.style.MIGRATE_HEADING("  HUMO TA'LIM -- FACE ID DAVOMAT TIZIMI SINXRONIZATSIYASI"))
        self.stdout.write(self.style.MIGRATE_HEADING("===============================================================\n"))

        settings_obj = FaceIDSettings.get_settings()
        self.stdout.write(f"Markaz: {settings_obj.center_name}")
        self.stdout.write(f"Koordinata: Lat {settings_obj.latitude}, Lon {settings_obj.longitude}")
        self.stdout.write(f"Masofa chegarasi: {settings_obj.allowed_radius_meters} metr")
        self.stdout.write(f"Standart ish vaqti: {settings_obj.default_work_start_time} - {settings_obj.default_work_end_time}")
        self.stdout.write(f"OpenCV biometrik tizimi: {'FAOL (OK)' if HAS_OPENCV else 'OGOHLANTIRISH: OpenCV topilmadi'}\n")

        synced_count = 0
        missing_photos = []
        ready_for_face_id = []

        # -- 1. MENEJERLARNI SINXRONLASH ------------------------------
        self.stdout.write(self.style.HTTP_INFO("-- 1. Menejerlar (Manager) tekshiruvi va sinxronizatsiyasi --"))
        
        # User role='manager' bo'lib, Manager profili bo'lmaganlarni to'g'rilash
        manager_users = CustomUser.objects.filter(role='manager')
        for u in manager_users:
            manager_prof = getattr(u, 'manager_profile', None)
            if not manager_prof:
                if not check_only:
                    manager_prof = Manager.objects.create(
                        user=u,
                        first_name=u.first_name or u.username,
                        last_name=u.last_name or '',
                        phone='',
                        generated_login=u.username,
                        generated_password='***',
                        work_start_time=settings_obj.default_work_start_time,
                        work_end_time=settings_obj.default_work_end_time,
                        late_fine_rate=settings_obj.default_late_fine_rate
                    )
                    self.stdout.write(self.style.SUCCESS(f"  [+] Menejer profili yaratildi: {u.username}"))
                else:
                    self.stdout.write(self.style.WARNING(f"  [!] Profil yetishmaydi: {u.username} (Menejer)"))

        managers = Manager.objects.all().select_related('user')
        for m in managers:
            synced_count += 1
            user = m.user
            if user.role != 'manager' and not check_only:
                user.role = 'manager'
                user.save(update_fields=['role'])

            has_photo = False
            face_detected = False

            if m.photo:
                try:
                    if os.path.exists(m.photo.path):
                        has_photo = True
                        if HAS_OPENCV and fs:
                            cv_img = fs._load_cv_image_from_file_or_path(m.photo.path)
                            if cv_img is not None:
                                roi, box = fs.extract_face_roi(cv_img)
                                if roi is not None:
                                    face_detected = True
                except Exception as e:
                    has_photo = False

            full_name = f"{m.first_name} {m.last_name}".strip() or m.user.username
            if has_photo and face_detected:
                ready_for_face_id.append(('Menejer', full_name, m.user.username, 'Face ID Tayyor (Biometrika OK)'))
                self.stdout.write(self.style.SUCCESS(f"  [OK] [Menejer] {full_name} (@{m.user.username}) -- Yuz rasmi va biometrika tayyor"))
            elif has_photo and not face_detected:
                missing_photos.append(('Menejer', full_name, m.user.username, 'Rasm bor, lekin yuz aniqlanmadi'))
                self.stdout.write(self.style.WARNING(f"  [!] [Menejer] {full_name} (@{m.user.username}) -- Rasm yuklangan, ammo yuz aniq ko'rinmayapti"))
            else:
                missing_photos.append(('Menejer', full_name, m.user.username, 'Rasm yuklanmagan'))
                self.stdout.write(self.style.NOTICE(f"  [-] [Menejer] {full_name} (@{m.user.username}) -- Face ID rasmi yuklanmagan"))

        # -- 2. O'QITUVCHILARNI SINXRONLASH ---------------------------
        self.stdout.write(self.style.HTTP_INFO("\n-- 2. O'qituvchilar (Teacher) tekshiruvi va sinxronizatsiyasi --"))
        
        # User role='teacher' bo'lib, Teacher profili bo'lmaganlarni to'g'rilash
        teacher_users = CustomUser.objects.filter(role='teacher')
        for u in teacher_users:
            teacher_prof = getattr(u, 'teacher_profile', None)
            if not teacher_prof:
                if not check_only:
                    teacher_prof = Teacher.objects.create(
                        user=u,
                        first_name=u.first_name or u.username,
                        last_name=u.last_name or '',
                        phone='',
                        generated_login=u.username,
                        generated_password='***',
                        work_start_time=settings_obj.default_work_start_time,
                        work_end_time=settings_obj.default_work_end_time,
                        late_fine_rate=settings_obj.default_late_fine_rate
                    )
                    self.stdout.write(self.style.SUCCESS(f"  [+] O'qituvchi profili yaratildi: {u.username}"))
                else:
                    self.stdout.write(self.style.WARNING(f"  [!] Profil yetishmaydi: {u.username} (O'qituvchi)"))

        teachers = Teacher.objects.all().select_related('user')
        for t in teachers:
            synced_count += 1
            user = t.user
            if user.role != 'teacher' and not check_only:
                user.role = 'teacher'
                user.save(update_fields=['role'])

            has_photo = False
            face_detected = False

            if t.photo:
                try:
                    if os.path.exists(t.photo.path):
                        has_photo = True
                        if HAS_OPENCV and fs:
                            cv_img = fs._load_cv_image_from_file_or_path(t.photo.path)
                            if cv_img is not None:
                                roi, box = fs.extract_face_roi(cv_img)
                                if roi is not None:
                                    face_detected = True
                except Exception as e:
                    has_photo = False

            full_name = f"{t.first_name} {t.last_name}".strip() or t.user.username
            if has_photo and face_detected:
                ready_for_face_id.append(("O'qituvchi", full_name, t.user.username, 'Face ID Tayyor (Biometrika OK)'))
                self.stdout.write(self.style.SUCCESS(f"  [OK] [O'qituvchi] {full_name} (@{t.user.username}) -- Yuz rasmi va biometrika tayyor"))
            elif has_photo and not face_detected:
                missing_photos.append(("O'qituvchi", full_name, t.user.username, 'Rasm bor, lekin yuz aniqlanmadi'))
                self.stdout.write(self.style.WARNING(f"  [!] [O'qituvchi] {full_name} (@{t.user.username}) -- Rasm yuklangan, ammo yuz aniq ko'rinmayapti"))
            else:
                missing_photos.append(("O'qituvchi", full_name, t.user.username, 'Rasm yuklanmagan'))
                self.stdout.write(self.style.NOTICE(f"  [-] [O'qituvchi] {full_name} (@{t.user.username}) -- Face ID rasmi yuklanmagan"))

        # -- 3. IRODATA: NAMUNA LOGLAR YARATISH ------------------------
        if generate_sample_logs and not check_only:
            self.stdout.write(self.style.HTTP_INFO("\n-- 3. Davomat jurnali uchun sinov yozuvlarini yaratish --"))
            today = date.today()
            all_staff_users = [m.user for m in managers] + [t.user for t in teachers]
            
            created_logs = 0
            for idx, u in enumerate(all_staff_users):
                log, created = FaceIDLog.objects.get_or_create(
                    user=u,
                    date=today,
                    defaults={
                        'check_in_time': timezone.now() - timedelta(hours=3 - idx),
                        'check_out_time': None,
                        'is_late': (idx % 2 == 1),
                        'late_minutes': 15 if (idx % 2 == 1) else 0,
                        'fine_percent_applied': Decimal('10.50') if (idx % 2 == 1) else Decimal('0.00'),
                        'fine_amount_deducted': Decimal('150000.00') if (idx % 2 == 1) else Decimal('0.00'),
                        'status': 'late' if (idx % 2 == 1) else 'on_time',
                        'distance_meters': 35.5,
                        'notes': "Sinxronizatsiya sinov yozuvi",
                    }
                )
                if created:
                    created_logs += 1

            self.stdout.write(self.style.SUCCESS(f"  [OK] Bugungi kun ({today}) uchun {created_logs} ta yangi sinov davomat yozuvi yaratildi!"))

        # -- XULOSA VA STATISTIKA --------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING("\n==============================================================="))
        self.stdout.write(self.style.MIGRATE_HEADING("  SINXRONIZATSIYA NATIJALARI"))
        self.stdout.write(self.style.MIGRATE_HEADING("==============================================================="))
        self.stdout.write(f"Jami tekshirilgan xodimlar: {synced_count} ta")
        self.stdout.write(self.style.SUCCESS(f"Face ID ga to'liq tayyor: {len(ready_for_face_id)} ta"))
        self.stdout.write(self.style.WARNING(f"Rasm yuklanishi kutilayotganlar: {len(missing_photos)} ta"))

        if missing_photos:
            self.stdout.write("\nFace ID ishlashi uchun quyidagi xodimlarga admin paneldan rasm yuklash kerak:")
            for role, name, uname, status in missing_photos:
                self.stdout.write(f"  - [{role}] {name} (login: {uname}) -> {status}")

        self.stdout.write(self.style.SUCCESS("\n[OK] Face ID sinxronizatsiyasi muvaffaqiyatli yakunlandi!\n"))
