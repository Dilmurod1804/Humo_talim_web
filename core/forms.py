import os
from decimal import Decimal
from django import forms
from .models import TemporaryStudent, MonthlyPayment, Teacher
from .validators import validate_latin_only, contains_cyrillic
from .services import optimize_student_photo
from .face_service import validate_face_photo, sync_staff_face_embedding


class TeacherProfileForm(forms.ModelForm):
    """
    O'qituvchi profili va fotosuratini tahrirlash (update) formasi.
    O'qituvchi profilini yangilashda yangi yozuv (dublikat) yaratilib ketmasligi uchun
    ushbu forma doimo `instance=teacher` parametri bilan chaqirilishi shart!
    """
    new_password = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={
            'class': 'form-control form-control-sm font-monospace',
            'placeholder': "Yangi parol kiriting yoki bo'sh qoldiring",
        }),
        label="Yangi parol (ixtiyoriy)"
    )
    monthly_salary = forms.DecimalField(
        required=False,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '1000'}),
        label="Oylik maosh stavkasi (so'm)"
    )
    late_fine_rate = forms.DecimalField(
        required=False,
        widget=forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1'}),
        label="Kechikish jarima foizi (%)"
    )
    work_start_time = forms.TimeField(
        required=False,
        widget=forms.TimeInput(attrs={'class': 'form-control form-control-sm', 'type': 'time'}),
    )
    work_end_time = forms.TimeField(
        required=False,
        widget=forms.TimeInput(attrs={'class': 'form-control form-control-sm', 'type': 'time'}),
    )
    photo = forms.ImageField(
        required=False,
        widget=forms.FileInput(attrs={'class': 'form-control form-control-sm', 'accept': 'image/*', 'id': 'photoInput', 'onchange': 'previewPhoto(event)'}),
    )

    class Meta:
        model = Teacher
        fields = [
            'first_name',
            'last_name',
            'phone',
            'work_start_time',
            'work_end_time',
            'monthly_salary',
            'late_fine_rate',
            'can_edit_students',
            'photo',
        ]
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control', 'required': 'required'}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'required': 'required'}),
            'phone': forms.TextInput(attrs={'class': 'form-control'}),
            'can_edit_students': forms.CheckboxInput(attrs={'class': 'form-check-input', 'id': 'canEditSwitch'}),
        }

    def clean_first_name(self):
        first_name = self.cleaned_data.get('first_name', '').strip()
        if contains_cyrillic(first_name):
            raise forms.ValidationError("Ismda faqat lotin alifbosidan foydalaning!")
        return first_name

    def clean_last_name(self):
        last_name = self.cleaned_data.get('last_name', '').strip()
        if contains_cyrillic(last_name):
            raise forms.ValidationError("Familiyada faqat lotin alifbosidan foydalaning!")
        return last_name

    def clean_photo(self):
        photo = self.cleaned_data.get('photo')
        if photo and hasattr(photo, 'file'):
            name = getattr(photo, 'name', '')
            ext = os.path.splitext(name)[1].lower() if name else ''
            if ext and ext not in ('.jpg', '.jpeg', '.png'):
                raise forms.ValidationError("Faqat .jpg, .jpeg, yoki .png formatdagi rasm yuklang.")
            try:
                opt_photo, err = optimize_student_photo(photo)
                if opt_photo:
                    return opt_photo
            except Exception:
                pass
        return photo

    def clean(self):
        cleaned_data = super().clean()
        if self.instance and self.instance.pk:
            if cleaned_data.get('monthly_salary') is None:
                cleaned_data['monthly_salary'] = self.instance.monthly_salary
            if cleaned_data.get('late_fine_rate') is None:
                cleaned_data['late_fine_rate'] = self.instance.late_fine_rate
            if not cleaned_data.get('work_start_time'):
                cleaned_data['work_start_time'] = self.instance.work_start_time
            if not cleaned_data.get('work_end_time'):
                cleaned_data['work_end_time'] = self.instance.work_end_time
        return cleaned_data

    def save(self, commit=True):
        teacher = super().save(commit=False)
        new_password = self.cleaned_data.get('new_password')
        if new_password and getattr(teacher, 'user', None):
            teacher.user.set_password(new_password)
            teacher.user.save()
            teacher.generated_password = new_password
        if commit:
            teacher.save()
            if teacher.photo and not teacher.has_face_embedding:
                sync_staff_face_embedding(teacher)
        return teacher


# Qulaylik va moslashuvchanlik uchun TeacherForm taxallusi
TeacherForm = TeacherProfileForm



class TemporaryStudentForm(forms.ModelForm):
    """Vaqtinchalik o'quvchini tez ro'yxatga olish formasi."""

    class Meta:
        model = TemporaryStudent
        fields = ['first_name', 'last_name', 'phone_1', 'phone_2', 'subject', 'preferred_time', 'notes']
        widgets = {
            'first_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Ism (lotin harflarida)',
            }),
            'last_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Familiya (lotin harflarida)',
            }),
            'phone_1': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '+998 90 123 45 67',
            }),
            'phone_2': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': '+998 ... (ixtiyoriy)',
            }),
            'subject': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': "Masalan: Ingliz tili",
            }),
            'preferred_time': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': "Masalan: 14:00 - 16:00",
            }),
            'notes': forms.Textarea(attrs={
                'class': 'form-control', 'rows': 2,
                'placeholder': "Qo'shimcha izoh...",
            }),
        }
        labels = {
            'first_name': 'Ism',
            'last_name': 'Familiya',
            'phone_1': 'Telefon 1',
            'phone_2': 'Telefon 2 (ixtiyoriy)',
            'subject': 'Fan (kurs)',
            'preferred_time': 'Kelgan / keladigan soat',
            'notes': 'Izoh',
        }

    def clean(self):
        cleaned_data = super().clean()
        for field in ['first_name', 'last_name', 'subject', 'preferred_time', 'notes']:
            val = cleaned_data.get(field)
            if val and contains_cyrillic(val):
                self.add_error(field, "Faqat lotin alifbosidagi harflardan foydalaning! Kirill harflari qabul qilinmaydi.")
        return cleaned_data


class MonthlyPaymentForm(forms.ModelForm):
    """To'lov qo'shish va tahrirlash formasi (to'lov turlari bilan)."""

    class Meta:
        model = MonthlyPayment
        fields = ['student', 'month', 'amount_paid', 'payment_type', 'payment_date', 'notes']
        widgets = {
            'student': forms.Select(attrs={'class': 'form-select'}),
            'month': forms.TextInput(attrs={'class': 'form-control', 'type': 'month'}),
            'amount_paid': forms.TextInput(attrs={'class': 'form-control', 'placeholder': "To'langan summa yoki matn"}),
            'payment_type': forms.Select(attrs={'class': 'form-select'}),
            'payment_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'notes': forms.TextInput(attrs={'class': 'form-control', 'placeholder': "Qo'shimcha izoh..."}),
        }
        labels = {
            'student': "O'quvchi",
            'month': "Oy",
            'amount_paid': "To'langan summa",
            'payment_type': "To'lov turi",
            'payment_date': "To'lov sanasi",
            'notes': "Izoh",
        }

