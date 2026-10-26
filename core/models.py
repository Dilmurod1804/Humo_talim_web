from datetime import date
from django.db import models
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.utils import timezone
from .validators import validate_latin_only

class CustomUser(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('manager', 'Menejer'),
        ('teacher', 'Teacher'),
        ('parent', 'Parent'),
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='parent')
    has_custom_credentials = models.BooleanField(default=False, help_text="Admin o'zi alohida login-parol yaratganmi?")

    @property
    def staff_profile(self):
        try:
            if hasattr(self, 'teacher_profile') and self.teacher_profile:
                return self.teacher_profile
        except Exception:
            pass
        try:
            if hasattr(self, 'manager_profile') and self.manager_profile:
                return self.manager_profile
        except Exception:
            pass
        if self.role == 'manager':
            from .models import Manager
            m, _ = Manager.objects.get_or_create(
                user=self,
                defaults={
                    'first_name': self.first_name or self.username,
                    'last_name': self.last_name or '',
                    'generated_login': self.username,
                    'generated_password': '***',
                }
            )
            return m
        return None

    @property
    def staff_full_name(self):
        sp = self.staff_profile
        if sp:
            return f"{sp.first_name} {sp.last_name}"
        return self.get_full_name() or self.username

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"

class Teacher(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='teacher_profile')
    first_name = models.CharField(max_length=100, validators=[validate_latin_only])
    last_name = models.CharField(max_length=100, validators=[validate_latin_only])
    phone = models.CharField(max_length=20, blank=True, null=True)
    generated_login = models.CharField(max_length=50)
    generated_password = models.CharField(max_length=50)
    can_edit_students = models.BooleanField(default=False, help_text="O'qituvchiga o'quvchilarni tahrirlash ruxsati")
    photo = models.ImageField(upload_to='staff_photos/', blank=True, null=True, help_text="Face ID yuz rasmi")
    monthly_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Oylik maosh stavkasi (so'm)")
    late_fine_rate = models.DecimalField(max_digits=5, decimal_places=2, default=2.0, help_text="Kechikish jarima foizi (%)")
    work_start_time = models.TimeField(default="08:00", help_text="Ish/Dars boshlanish vaqti")
    work_end_time = models.TimeField(default="18:00", help_text="Ish/Dars tugash vaqti")
    is_active = models.BooleanField(default=True)

    @property
    def can_edit(self):
        return bool(self.can_edit_students)

    @can_edit.setter
    def can_edit(self, value):
        self.can_edit_students = bool(value)

    @property
    def role_title(self):
        return "O'qituvchi"

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

class Manager(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='manager_profile')
    first_name = models.CharField(max_length=100, validators=[validate_latin_only])
    last_name = models.CharField(max_length=100, validators=[validate_latin_only])
    phone = models.CharField(max_length=20, blank=True, null=True)
    generated_login = models.CharField(max_length=50)
    generated_password = models.CharField(max_length=50)
    photo = models.ImageField(upload_to='staff_photos/', blank=True, null=True, help_text="Face ID yuz rasmi")
    monthly_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Oylik maosh stavkasi (so'm)")
    late_fine_rate = models.DecimalField(max_digits=5, decimal_places=2, default=2.0, help_text="Kechikish jarima foizi (%)")
    work_start_time = models.TimeField(default="08:00", help_text="Ish boshlanish vaqti")
    work_end_time = models.TimeField(default="18:00", help_text="Ish tugash vaqti")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def role_title(self):
        return "Menejer"

    def __str__(self):
        return f"{self.first_name} {self.last_name} (Menejer)"

class FaceIDSettings(models.Model):
    center_name = models.CharField(max_length=150, default="HummoKids Bog'chasi")
    latitude = models.DecimalField(max_digits=12, decimal_places=8, default=41.31108100, help_text="Markaz binosi koordinatasi (Latitude)")
    longitude = models.DecimalField(max_digits=12, decimal_places=8, default=69.24056200, help_text="Markaz binosi koordinatasi (Longitude)")
    allowed_radius_meters = models.IntegerField(default=150, help_text="Ruxsat etilgan masofa chegarasi (metrda)")
    default_work_start_time = models.TimeField(default="08:00", help_text="Standart ish boshlanish vaqti")
    default_work_end_time = models.TimeField(default="18:00", help_text="Standart ish tugash vaqti")
    default_late_fine_rate = models.DecimalField(max_digits=5, decimal_places=2, default=2.0, help_text="Standart kechikish jarima foizi (%)")
    is_geofencing_enabled = models.BooleanField(default=True, help_text="Geolokatsiya chegarasi tekshiruvi yoqilganmi?")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_settings(cls):
        obj = cls.objects.first()
        if not obj:
            obj = cls.objects.create()
        return obj

    @property
    def geofence_radius(self):
        """Geofencing ruxsat etilgan masofa radiusi (metrda) alias."""
        return self.allowed_radius_meters

    @geofence_radius.setter
    def geofence_radius(self, value):
        self.allowed_radius_meters = int(value)

    def __str__(self):
        return f"{self.center_name} Face ID ({self.allowed_radius_meters}m)"

class FaceIDLog(models.Model):
    STATUS_CHOICES = (
        ('on_time', "O'z vaqtida"),
        ('late', "Kechikdi"),
        ('excused', "Sababli"),
    )
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='face_logs')
    date = models.DateField(default=date.today)
    check_in_time = models.DateTimeField(null=True, blank=True, help_text="Kelgan vaqti")
    check_out_time = models.DateTimeField(null=True, blank=True, help_text="Ketgan vaqti")
    is_late = models.BooleanField(default=False)
    late_minutes = models.IntegerField(default=0, help_text="Kechikish daqiqalari")
    fine_percent_applied = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Ushlab qolingan jarima foizi")
    fine_amount_deducted = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Ushlab qolingan jarima summasi (so'm)")
    verified_photo = models.ImageField(upload_to='face_scans/', blank=True, null=True, help_text="Scan qilingan rasm")
    latitude = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True)
    longitude = models.DecimalField(max_digits=12, decimal_places=8, null=True, blank=True)
    distance_meters = models.FloatField(null=True, blank=True, help_text="Markazdan masofasi (metrda)")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='on_time')
    notes = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('user', 'date')
        ordering = ['-date', '-check_in_time']

    @property
    def staff_name(self):
        return self.user.staff_full_name

    @property
    def staff_role(self):
        sp = self.user.staff_profile
        if sp and hasattr(sp, 'role_title'):
            return sp.role_title
        if self.user.role == 'manager':
            return "Menejer"
        elif self.user.role == 'teacher':
            return "O'qituvchi"
        return self.user.get_role_display()

    def __str__(self):
        return f"{self.user.username} - {self.date} ({self.get_status_display()})"

class Group(models.Model):
    name = models.CharField(max_length=100, validators=[validate_latin_only]) # e.g., Ingliz tili
    subject = models.CharField(max_length=100, blank=True, default='', validators=[validate_latin_only], help_text="Fan nomi")
    teacher = models.ForeignKey(Teacher, on_delete=models.SET_NULL, null=True, related_name='groups')

    @property
    def subject_name(self):
        return self.subject.strip() if self.subject and self.subject.strip() else self.name

    def __str__(self):
        return self.name

class TimeSlot(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name='time_slots')
    days = models.CharField(max_length=100, default="Dushanba, Chorshanba, Juma", validators=[validate_latin_only])
    start_time = models.TimeField()
    end_time = models.TimeField()

    def __str__(self):
        return f"{self.group.name} | {self.days} | {self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')}"

class Student(models.Model):
    first_name = models.CharField(max_length=100, validators=[validate_latin_only])
    last_name = models.CharField(max_length=100, validators=[validate_latin_only])
    age = models.IntegerField(blank=True, null=True)
    phone_1 = models.CharField(max_length=20)
    phone_2 = models.CharField(max_length=20, blank=True, null=True)
    payment_rate = models.IntegerField(default=0, help_text="Oylik to'lov summasi")
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.SET_NULL, null=True, related_name='students')
    joined_date = models.DateField(default=date.today, help_text="Guruhga qo'shilgan sana")
    is_active = models.BooleanField(default=True)
    left_date = models.DateField(null=True, blank=True, help_text="Guruhdan o'chirilgan/chiqqan sana")
    is_local = models.BooleanField(default=False, verbose_name="Mahalladan keladigan o'quvchi")
    photo = models.ImageField(upload_to='students/photos/', blank=True, null=True, help_text="O'quvchi rasmi (ixtiyoriy)")

    @property
    def is_mahalla(self):
        """Mahalladan keladigan o'quvchi belgisi (is_local uchun qulay alias)."""
        return self.is_local

    @is_mahalla.setter
    def is_mahalla(self, value):
        self.is_local = bool(value)

    @property
    def is_archived(self):
        """Arxivlanganlik holati (is_active=False bo'lsa True)."""
        return not self.is_active

    @is_archived.setter
    def is_archived(self, value):
        self.is_active = not bool(value)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

class Attendance(models.Model):
    STATUS_CHOICES = (
        ('present', 'Keldi (+)'),
        ('absent', 'Kelmadi (-)'),
    )
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='attendances')
    date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)

    class Meta:
        unique_together = ('student', 'date')

    def clean(self):
        super().clean()
        if self.date:
            today = timezone.now().date()
            if self.date != today:
                raise ValidationError("Yo'qlama faqat joriy kun (bugun) uchun kiritilishi mumkin!")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.student} - {self.date} - {self.status}"

class DeletedStudent(models.Model):
    # Old text fields (kept for backwards compatibility or simple display)
    student_name = models.CharField(max_length=200)
    phones = models.CharField(max_length=100)
    group_name = models.CharField(max_length=100)
    reason = models.TextField(blank=True, default='')
    deleted_at = models.DateTimeField(auto_now_add=True)

    # New fields to allow restoration
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    age = models.IntegerField(blank=True, null=True)
    phone_1 = models.CharField(max_length=20, blank=True, null=True)
    phone_2 = models.CharField(max_length=20, blank=True, null=True)
    time_slot_id = models.IntegerField(blank=True, null=True)
    student_id = models.IntegerField(blank=True, null=True, help_text="Asosiy Student ID si")

    def __str__(self):
        return self.student_name


class TemporaryStudent(models.Model):
    """Vaqtinchalik ro'yxatga olingan o'quvchilar — hali guruhga biriktirilmagan."""
    first_name = models.CharField(max_length=100, validators=[validate_latin_only])
    last_name = models.CharField(max_length=100, validators=[validate_latin_only])
    phone_1 = models.CharField(max_length=20)
    phone_2 = models.CharField(max_length=20, blank=True, default='')
    subject = models.CharField(max_length=100, help_text="Qaysi fan / kurs", validators=[validate_latin_only])
    preferred_time = models.CharField(max_length=100, blank=True, default='', help_text="Qaysi soatga kelgan", validators=[validate_latin_only])
    notes = models.TextField(blank=True, default='', validators=[validate_latin_only])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.first_name} {self.last_name} — {self.subject}"


class MonthlyPayment(models.Model):
    """O'quvchining ma'lum bir oy uchun to'lov ma'lumoti"""
    STATUS_CHOICES = (
        ('paid', "To'langan"),
        ('partial', "Qisman to'langan"),
        ('unpaid', "To'lanmagan"),
    )
    PAYMENT_TYPE_CHOICES = (
        ('cash', 'Naqd pul (Cash)'),
        ('payme', 'Payme'),
        ('click', 'Click'),
        ('terminal', 'Terminal / Plastik karta'),
    )
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='monthly_payments')
    month = models.CharField(max_length=7, help_text="Yil va Oy. Masalan: 2026-09")
    amount_paid = models.CharField(max_length=100, default='', help_text="To'lov summasi yoki izoh")
    payment_type = models.CharField(
        max_length=20,
        choices=PAYMENT_TYPE_CHOICES,
        default='cash',
        verbose_name="To'lov turi",
        help_text="To'lov usuli (Naqd, Payme, Click, Terminal)"
    )
    payment_date = models.DateField(null=True, blank=True, help_text="To'lov qilingan sana")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='paid')
    notes = models.CharField(max_length=255, blank=True, default='', help_text="Izoh yoki to'lov turi")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('student', 'month')

    def __str__(self):
        return f"{self.student} - {self.month} ({self.amount_paid})"

class Message(models.Model):
    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_messages')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_messages')
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"From {self.sender} to {self.receiver} at {self.timestamp.strftime('%Y-%m-%d %H:%M')}"


class StaffBonus(models.Model):
    """Direktor / Admin tomonidan xodimga berilgan qo'shimcha bonuslar (erta kelganlik yoki faollik uchun)."""
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='bonuses')
    month = models.CharField(max_length=7, help_text="Yil va Oy (YYYY-MM)")
    date = models.DateField(default=date.today)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Bonus summasi (so'm)")
    reason = models.CharField(max_length=255, default="Erta kelganlik / Faollik uchun bonus")
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='given_bonuses')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.month}: +{self.amount} so'm ({self.reason})"


class AdvanceRequest(models.Model):
    """Xodimlar tomonidan oylikdan avans so'rash moduli."""
    STATUS_CHOICES = (
        ('pending', "Kutilmoqda"),
        ('approved', "Tasdiqlandi"),
        ('rejected', "Rad etildi"),
    )
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='advance_requests')
    amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="So'ralgan avans summasi (so'm)")
    reason = models.TextField(blank=True, default='', help_text="Avans so'rash sababi")
    month = models.CharField(max_length=7, help_text="Qaysi oy maoshidan ushlab qolinadi (YYYY-MM)")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    admin_comment = models.TextField(blank=True, default='', help_text="Direktor yoki Menejer izohi")
    reviewed_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_advances')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user.username} - {self.month}: {self.amount:,.0f} so'm ({self.get_status_display()})"


class AuditLog(models.Model):
    """
    Tizim Tarixi (Audit Log):
    - Face ID keldi-ketdi
    - Bolalar davomati
    - Ushlab qolingan shtraflar
    - Berilgan bonuslar
    - Xodim avanslari
    - Qilingan to'lovlar
    - Tizim boshqaruvi harakatlari
    """
    ACTION_CHOICES = (
        ('face_id', 'Face ID Keldi-Ketdi'),
        ('attendance', 'Bolalar Davomati'),
        ('fine', 'Ushlab qolingan Shtraf'),
        ('bonus', 'Berilgan Bonus'),
        ('advance', 'Xodim Avansi'),
        ('payment', "Qilingan To'lov"),
        ('system', 'Tizim Boshqaruvi'),
    )
    action_type = models.CharField(max_length=30, choices=ACTION_CHOICES)
    actor = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='audit_actions')
    actor_name = models.CharField(max_length=150, blank=True, default='')
    title = models.CharField(max_length=255)
    details = models.TextField(blank=True, default='')
    ip_address = models.CharField(max_length=50, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"[{self.get_action_type_display()}] {self.title} ({self.created_at.strftime('%Y-%m-%d %H:%M')})"

