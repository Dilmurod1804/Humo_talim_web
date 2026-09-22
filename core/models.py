from django.db import models
from django.contrib.auth.models import AbstractUser

class CustomUser(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('teacher', 'Teacher'),
        ('parent', 'Parent'),
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='parent')
    has_custom_credentials = models.BooleanField(default=False, help_text="Admin o'zi alohida login-parol yaratganmi?")

    def __str__(self):
        return f"{self.username} ({self.get_role_display()})"

class Teacher(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE, related_name='teacher_profile')
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, blank=True, null=True)
    generated_login = models.CharField(max_length=50)
    generated_password = models.CharField(max_length=50)

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

class Group(models.Model):
    name = models.CharField(max_length=100) # e.g., Ingliz tili
    teacher = models.ForeignKey(Teacher, on_delete=models.SET_NULL, null=True, related_name='groups')

    def __str__(self):
        return self.name

class TimeSlot(models.Model):
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name='time_slots')
    days = models.CharField(max_length=100, default="Dushanba, Chorshanba, Juma")
    start_time = models.TimeField()
    end_time = models.TimeField()

    def __str__(self):
        return f"{self.group.name} | {self.days} | {self.start_time.strftime('%H:%M')} - {self.end_time.strftime('%H:%M')}"

class Student(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    age = models.IntegerField()
    phone_1 = models.CharField(max_length=20)
    phone_2 = models.CharField(max_length=20, blank=True, null=True)
    payment_rate = models.IntegerField(default=0, help_text="Oylik to'lov summasi")
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.SET_NULL, null=True, related_name='students')
    joined_date = models.DateField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

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

    def __str__(self):
        return f"{self.student} - {self.date} - {self.status}"

class DeletedStudent(models.Model):
    # Old text fields (kept for backwards compatibility or simple display)
    student_name = models.CharField(max_length=200)
    phones = models.CharField(max_length=100)
    group_name = models.CharField(max_length=100)
    reason = models.TextField()
    deleted_at = models.DateTimeField(auto_now_add=True)

    # New fields to allow restoration
    first_name = models.CharField(max_length=100, blank=True, null=True)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    age = models.IntegerField(blank=True, null=True)
    phone_1 = models.CharField(max_length=20, blank=True, null=True)
    phone_2 = models.CharField(max_length=20, blank=True, null=True)
    time_slot_id = models.IntegerField(blank=True, null=True)

    def __str__(self):
        return self.student_name


class TemporaryStudent(models.Model):
    """Vaqtinchalik ro'yxatga olingan o'quvchilar — hali guruhga biriktirilmagan."""
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone_1 = models.CharField(max_length=20)
    phone_2 = models.CharField(max_length=20, blank=True, default='')
    subject = models.CharField(max_length=100, help_text="Qaysi fan / kurs")
    preferred_time = models.CharField(max_length=100, blank=True, default='', help_text="Qaysi soatga kelgan")
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.first_name} {self.last_name} — {self.subject}"


class MonthlyPayment(models.Model):
    """O'quvchining ma'lum bir oy uchun to'lov ma'lumoti"""
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='monthly_payments')
    month = models.CharField(max_length=7, help_text="Yil va Oy. Masalan: 2026-09")
    amount_paid = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('student', 'month')

    def __str__(self):
        return f"{self.student} - {self.month} ({self.amount_paid})"
