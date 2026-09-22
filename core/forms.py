from django import forms
from .models import TemporaryStudent


class TemporaryStudentForm(forms.ModelForm):
    """Vaqtinchalik o'quvchini tez ro'yxatga olish formasi."""

    class Meta:
        model = TemporaryStudent
        fields = ['first_name', 'last_name', 'phone_1', 'phone_2', 'subject', 'preferred_time', 'notes']
        widgets = {
            'first_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Ism',
            }),
            'last_name': forms.TextInput(attrs={
                'class': 'form-control', 'placeholder': 'Familiya',
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
