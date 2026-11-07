from django import forms
from .models import TemporaryStudent, MonthlyPayment
from .validators import validate_latin_only, contains_cyrillic


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

